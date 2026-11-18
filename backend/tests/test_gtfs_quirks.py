"""Quirks seen in real publishers' feeds."""

from __future__ import annotations

import io
import zipfile

from app.agents.transit.gtfs_static import _rows


def make_zip(content: str) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("routes.txt", content)
    return zipfile.ZipFile(buf)


def test_a_blank_line_before_the_header_is_ignored() -> None:
    rows = list(_rows(make_zip("\nroute_id,route_type\n1,3\n\n2,3\n"), "routes.txt"))
    assert rows == [{"route_id": "1", "route_type": "3"}, {"route_id": "2", "route_type": "3"}]


def test_a_byte_order_mark_and_quoted_commas_still_work() -> None:
    rows = list(
        _rows(make_zip('\ufeffroute_id,route_long_name\n1,"Braitling, Ciccone"\n'), "routes.txt")
    )
    assert rows == [{"route_id": "1", "route_long_name": "Braitling, Ciccone"}]


def test_padded_header_names_are_trimmed() -> None:
    rows = list(_rows(make_zip("location_type, parent_station, stop_id\n0,,10000\n"), "routes.txt"))
    assert rows == [{"location_type": "0", "parent_station": "", "stop_id": "10000"}]
