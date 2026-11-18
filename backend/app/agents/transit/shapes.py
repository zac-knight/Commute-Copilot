"""Road/track geometry for services, from the feed's ``shapes.txt``.

The main GTFS database does not keep shapes (they are 40 MB and only the map needs them), so this
builds a small sidecar SQLite index from the downloaded zip the first time it is needed (a few
seconds, in a background thread) and rebuilds it when the zip changes. Until it is ready, or when
a trip has no shape, callers fall back to straight lines between stops.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import zipfile
from pathlib import Path

from app.agents.transit.gtfs_static import _rows

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE shapes (
    shape_id TEXT NOT NULL, seq INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL);
CREATE TABLE trip_shape (trip_id TEXT PRIMARY KEY, shape_id TEXT NOT NULL);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""
Pt = tuple[float, float]


class ShapeStore:
    def __init__(self, zip_path: Path, db_path: Path) -> None:
        self.zip_path, self.db_path = zip_path, db_path
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- building

    def _stamp(self) -> str:
        st = self.zip_path.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def ready(self) -> bool:
        if not self.db_path.exists() or not self.zip_path.exists():
            return self.db_path.exists() and not self.zip_path.exists()
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as c:
                row = c.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()
            return bool(row) and row[0] == self._stamp()
        except sqlite3.Error:
            return False

    def build(self) -> None:
        """Synchronous build (the tests call this; the app uses ``start``)."""
        if not self.zip_path.exists():
            return
        tmp = self.db_path.with_suffix(".building")
        tmp.unlink(missing_ok=True)
        try:
            built = self._build_into(tmp)
        except BaseException:
            tmp.unlink(missing_ok=True)  # never leave a half-built index behind
            raise
        if built:
            tmp.replace(self.db_path)
            log.info("shape index built: %s", self.db_path)

    def _build_into(self, tmp: Path) -> bool:
        """Write the index to ``tmp``; False when the feed has no shapes to index."""
        with zipfile.ZipFile(self.zip_path) as zf:
            names = set(zf.namelist())
            if "shapes.txt" not in names or "trips.txt" not in names:
                return False
            conn = sqlite3.connect(tmp)
            try:
                conn.executescript(SCHEMA)
                batch: list[tuple[str, int, float, float]] = []
                for r in _rows(zf, "shapes.txt"):
                    batch.append((r["shape_id"], int(r["shape_pt_sequence"]),
                                  float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))  # fmt: skip
                    if len(batch) >= 50_000:
                        conn.executemany("INSERT INTO shapes VALUES (?,?,?,?)", batch)
                        batch.clear()
                conn.executemany("INSERT INTO shapes VALUES (?,?,?,?)", batch)
                conn.execute("CREATE INDEX idx_shapes ON shapes(shape_id, seq)")
                trips = (
                    (r["trip_id"], r["shape_id"])
                    for r in _rows(zf, "trips.txt")
                    if r.get("shape_id")
                )
                conn.executemany("INSERT OR IGNORE INTO trip_shape VALUES (?,?)", trips)
                conn.execute("INSERT INTO meta VALUES ('stamp', ?)", (self._stamp(),))
                conn.commit()
            finally:
                conn.close()
        return True

    def start(self) -> None:
        """Build in the background if missing or stale (idempotent)."""
        with self._lock:
            if self.ready() or (self._thread and self._thread.is_alive()):
                return

            def work() -> None:
                try:
                    self.build()
                except Exception:
                    log.exception("shape index build failed")

            self._thread = threading.Thread(target=work, name="shape-index", daemon=True)
            self._thread.start()

    # ---------------------------------------------------------------- lookup

    def path_for_trip(self, trip_id: str) -> list[Pt] | None:
        """The service's full polyline, or None (not built yet / no shape)."""
        if not self.db_path.exists():
            self.start()
            return None
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as c:
                rows = c.execute(
                    "SELECT s.lat, s.lon FROM trip_shape t JOIN shapes s ON s.shape_id=t.shape_id"
                    " WHERE t.trip_id=? ORDER BY s.seq",
                    (trip_id,),
                ).fetchall()
        except sqlite3.Error:
            return None
        return [(float(a), float(b)) for a, b in rows] or None


def _d2(a: Pt, b: Pt, k: float) -> float:
    return ((a[0] - b[0]) ** 2) + (((a[1] - b[1]) * k) ** 2)


def clip(path: list[Pt], start: Pt, end: Pt) -> list[Pt] | None:
    """The part of ``path`` between the point nearest ``start`` and the one nearest ``end`` after
    it, bracketed by the exact stop positions. None when that cannot be done sensibly."""
    if len(path) < 2:
        return None
    k = math.cos(math.radians(start[0]))
    i = min(range(len(path)), key=lambda n: _d2(path[n], start, k))
    rest = range(i + 1, len(path))
    if not rest:
        return None
    j = min(rest, key=lambda n: _d2(path[n], end, k))
    return [start, *path[i + 1 : j], end] if j > i + 1 else [start, end]
