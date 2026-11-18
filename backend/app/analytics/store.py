"""SQLite store for polled realtime observations, polls and hourly rain (Phase 8: data science).

``obs`` keeps every observation (one row per departure per poll) so analysis can reconstruct what was
known at any time; the analysis picks the LAST observation near departure as the 'final' delay.
All data are public: TransLink GTFS-Realtime (via the existing transit service) and Open-Meteo.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS obs (
    ts INTEGER NOT NULL,          -- when we observed (epoch s)
    trip_id TEXT NOT NULL, route TEXT NOT NULL, stop_id TEXT NOT NULL, service_date TEXT NOT NULL,
    sched INTEGER NOT NULL,       -- scheduled departure (epoch s)
    pred INTEGER,                 -- predicted departure (epoch s), NULL if none
    delay_s INTEGER,              -- NULL when there is no realtime estimate
    rel TEXT NOT NULL,            -- SCHEDULED | CANCELED | NO_DATA | ADDED
    basis TEXT                    -- reported | estimated_from_earlier_stop | NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_key ON obs(trip_id, stop_id, service_date, ts);
CREATE INDEX IF NOT EXISTS idx_obs_sched ON obs(sched);
CREATE TABLE IF NOT EXISTS polls (ts INTEGER NOT NULL, ok INTEGER NOT NULL, n INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS rain (slot INTEGER PRIMARY KEY, mm REAL NOT NULL);  -- slot = epoch s of the hour END
"""


@dataclass(frozen=True)
class Obs:
    ts: int
    trip_id: str
    route: str
    stop_id: str
    service_date: str
    sched: int
    pred: int | None
    delay_s: int | None
    rel: str
    basis: str | None = None


class DelayStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def add_obs(self, rows: Iterable[Obs]) -> int:
        data = [(o.ts, o.trip_id, o.route, o.stop_id, o.service_date, o.sched, o.pred, o.delay_s,
                 o.rel, o.basis) for o in rows]  # fmt: skip
        with self._lock, self._conn:
            self._conn.executemany("INSERT INTO obs VALUES (?,?,?,?,?,?,?,?,?,?)", data)
        return len(data)

    def add_poll(self, ts: int, ok: bool, n: int, error: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO polls VALUES (?,?,?,?)", (ts, int(ok), n, error))

    def add_rain(self, slots: dict[int, float]) -> None:
        with self._lock, self._conn:
            self._conn.executemany("INSERT OR REPLACE INTO rain VALUES (?,?)", list(slots.items()))

    def rain(self) -> dict[int, float]:
        with self._lock:
            return dict(self._conn.execute("SELECT slot, mm FROM rain").fetchall())

    def observations(self) -> list[Obs]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts,trip_id,route,stop_id,service_date,sched,pred,delay_s,rel,basis "
                "FROM obs ORDER BY ts"
            ).fetchall()
        return [Obs(*r) for r in rows]

    def poll_stats(self) -> dict[str, int | None]:
        with self._lock:
            n, ok, first, last = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(ok),0), MIN(ts), MAX(ts) FROM polls"
            ).fetchone()
            rows = self._conn.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
        return {"polls": n, "ok_polls": ok, "first_ts": first, "last_ts": last, "rows": rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
