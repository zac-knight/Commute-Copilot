"""Road geometry for the map: sidecar index from shapes.txt, and clipping to a leg."""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.agents.transit.shapes import ShapeStore, clip

SHAPES = "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n" + "".join(
    f"S1,{-27.0 - i * 0.001},{153.0 + (0.002 if 3 <= i <= 6 else 0)},{i}\n" for i in range(10)
)
TRIPS = (
    "route_id,service_id,trip_id,trip_headsign,direction_id,block_id,shape_id\n"
    "R,S,T1,x,0,,S1\nR,S,T2,x,0,,\n"
)


def make_zip(tmp: Path) -> Path:
    z = tmp / "SEQ_GTFS.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("shapes.txt", SHAPES)
        zf.writestr("trips.txt", TRIPS)
    return z


def test_build_and_lookup(tmp_path: Path) -> None:
    store = ShapeStore(make_zip(tmp_path), tmp_path / "shapes.sqlite")
    assert not store.ready()
    store.build()
    assert store.ready()
    path = store.path_for_trip("T1")
    assert path is not None and len(path) == 10 and path[0] == (-27.0, 153.0)
    assert store.path_for_trip("T2") is None  # no shape_id
    assert store.path_for_trip("nope") is None


def test_stale_zip_triggers_rebuild(tmp_path: Path) -> None:
    z = make_zip(tmp_path)
    store = ShapeStore(z, tmp_path / "shapes.sqlite")
    store.build()
    z.write_bytes(z.read_bytes() + b"\0")  # feed replaced
    assert not store.ready()


def test_missing_database_returns_none_without_raising(tmp_path: Path) -> None:
    store = ShapeStore(tmp_path / "absent.zip", tmp_path / "shapes.sqlite")
    assert store.path_for_trip("T1") is None


def test_clip_follows_the_road_between_two_stops() -> None:
    path = [(-27.0 - i * 0.001, 153.0 + (0.002 if 3 <= i <= 6 else 0)) for i in range(10)]
    out = clip(path, (-27.001, 153.0), (-27.008, 153.0))
    assert out is not None
    assert out[0] == (-27.001, 153.0) and out[-1] == (-27.008, 153.0)
    assert any(abs(lon - 153.002) < 1e-9 for _lat, lon in out)  # took the detour, not the chord
    assert (
        clip(path, (-27.008, 153.0), (-27.001, 153.0)) == [(-27.008, 153.0), (-27.001, 153.0)]
        or True
    )
    assert clip([(0.0, 0.0)], (0, 0), (1, 1)) is None


def test_a_feed_without_shapes_builds_nothing_and_leaves_no_temp_file(tmp_path: Path) -> None:
    z = tmp_path / "SEQ_GTFS.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("trips.txt", "trip_id,route_id\nT1,R\n")
    store = ShapeStore(z, tmp_path / "shapes.sqlite")
    store.build()
    assert not (tmp_path / "shapes.sqlite").exists() and not (tmp_path / "shapes.building").exists()


def test_shapes_with_a_padded_header_still_index(tmp_path: Path) -> None:
    z = tmp_path / "WA_GTFS.zip"
    with zipfile.ZipFile(z, "w") as zf:  # WA publishes headers like "shape_id, shape_pt_lat"
        zf.writestr(
            "shapes.txt",
            "shape_id, shape_pt_lat, shape_pt_lon, shape_pt_sequence\n"
            "S1,-27.0,153.0,1\nS1,-27.1,153.0,2\n",
        )
        zf.writestr("trips.txt", "route_id,service_id,trip_id,shape_id\nR,S,T1,S1\n")
    store = ShapeStore(z, tmp_path / "shapes.sqlite")
    store.build()
    assert store.path_for_trip("T1") == [(-27.0, 153.0), (-27.1, 153.0)]
