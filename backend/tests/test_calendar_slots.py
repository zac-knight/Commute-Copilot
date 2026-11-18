from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from app.agents.calendar.slots import Interval, busy_intervals, conflicts_for, find_free_slots
from tests.calendar_fixtures import all_day, at, ev

WH = (time(9, 0), time(17, 0))


def spans(slots: list[Interval]) -> list[tuple[str, str]]:
    return [(f"{s.start:%a %H:%M}", f"{s.end:%H:%M}") for s in slots]


def free(
    events: list, start: datetime, end: datetime, dur: int = 60, **kw: object
) -> list[Interval]:
    kw.setdefault("working_hours", WH)
    return find_free_slots(events, start, end, dur, **kw)  # type: ignore[arg-type]


def test_empty_calendar_gives_the_whole_working_day() -> None:
    assert spans(free([], at(0), at(23, 59))) == [("Mon 09:00", "17:00")]


def test_event_splits_the_day() -> None:
    slots = free([ev("Meeting", at(10), at(11))], at(0), at(23, 59))
    assert spans(slots) == [("Mon 09:00", "10:00"), ("Mon 11:00", "17:00")]


def test_back_to_back_events_leave_no_gap() -> None:
    evs = [ev("A", at(9), at(10)), ev("B", at(10), at(11)), ev("C", at(11), at(12))]
    assert spans(free(evs, at(0), at(23, 59))) == [("Mon 12:00", "17:00")]


def test_overlapping_events_merge() -> None:
    evs = [ev("A", at(10), at(12)), ev("B", at(11), at(13)), ev("C", at(10, 30), at(11, 15))]
    assert spans(free(evs, at(0), at(23, 59))) == [("Mon 09:00", "10:00"), ("Mon 13:00", "17:00")]


def test_event_containing_another_is_not_double_counted() -> None:
    evs = [ev("Big", at(10), at(15)), ev("Small", at(11), at(12))]
    assert spans(free(evs, at(0), at(23, 59))) == [("Mon 09:00", "10:00"), ("Mon 15:00", "17:00")]


def test_duration_filters_short_gaps() -> None:
    evs = [ev("A", at(9), at(10)), ev("B", at(10, 30), at(16))]  # 30 min gap, then 16:00-17:00
    assert spans(free(evs, at(0), at(23, 59), 60)) == [("Mon 16:00", "17:00")]
    assert spans(free(evs, at(0), at(23, 59), 30)) == [
        ("Mon 10:00", "10:30"),
        ("Mon 16:00", "17:00"),
    ]
    assert free(evs, at(0), at(23, 59), 61) == []  # exactly-fitting gaps count, longer ones do not


def test_exact_fit_counts() -> None:
    assert spans(free([ev("A", at(9), at(16))], at(0), at(23, 59), 60)) == [("Mon 16:00", "17:00")]


def test_no_free_slot() -> None:
    assert free([ev("All day", at(8), at(18))], at(0), at(23, 59)) == []
    assert free([], at(0), at(23, 59), 9 * 60) == []  # a 9 h task cannot fit in an 8 h day


def test_buffer_pads_timed_busy_events_but_not_the_window_edges() -> None:
    evs = [ev("A", at(10), at(11)), ev("B", at(13), at(14))]
    slots = free(evs, at(0), at(23, 59), 30, buffer_minutes=15)
    assert spans(slots) == [("Mon 09:00", "09:45"), ("Mon 11:15", "12:45"), ("Mon 14:15", "17:00")]


def test_buffer_can_remove_a_gap_entirely() -> None:
    evs = [ev("A", at(9), at(10)), ev("B", at(10, 20), at(17))]
    assert free(evs, at(0), at(23, 59), 10, buffer_minutes=15) == []
    assert spans(free(evs, at(0), at(23, 59), 10)) == [("Mon 10:00", "10:20")]


def test_working_hours_and_whole_day() -> None:
    assert spans(free([], at(0), at(23, 59), working_hours=(time(12), time(13)))) == [
        ("Mon 12:00", "13:00")
    ]
    assert spans(free([], at(0), at(23, 59), working_hours=None)) == [("Mon 00:00", "23:59")]
    # an event before work hours does not matter
    assert spans(free([ev("Gym", at(6), at(7))], at(0), at(23, 59))) == [("Mon 09:00", "17:00")]


def test_event_straddling_the_working_hours_edge() -> None:
    evs = [ev("Early", at(8), at(9, 30)), ev("Late", at(16, 30), at(18))]
    assert spans(free(evs, at(0), at(23, 59))) == [("Mon 09:30", "16:30")]


def test_range_clips_the_windows() -> None:
    assert spans(free([], at(13), at(15))) == [("Mon 13:00", "15:00")]  # 'tomorrow afternoon'-style
    assert free([], at(17, 30), at(20)) == []  # entirely outside working hours


def test_multi_day_range_and_multi_day_event() -> None:
    conf = ev("Conference", at(14), at(11, day=2))  # Mon 14:00 -> Wed 11:00
    slots = free([conf], at(0), at(23, 59, day=2))
    assert spans(slots) == [("Mon 09:00", "14:00"), ("Wed 11:00", "17:00")]


def test_event_overnight() -> None:
    night = ev("Flight", at(22), at(6, day=1))
    assert spans(free([night], at(0), at(23, 59, day=1))) == [
        ("Mon 09:00", "17:00"),
        ("Tue 09:00", "17:00"),
    ]


# --- all-day and free events --------------------------------------------------------------


def test_all_day_events_do_not_block_by_default() -> None:
    assert spans(free([all_day("Birthday", 0)], at(0), at(23, 59))) == [("Mon 09:00", "17:00")]


def test_all_day_events_block_when_asked() -> None:
    evs = [all_day("Away", 0, days=2)]  # Mon and Tue
    slots = free(evs, at(0), at(23, 59, day=2), all_day_blocks=True)
    assert spans(slots) == [("Wed 09:00", "17:00")]


def test_free_timed_events_never_block() -> None:
    assert spans(free([ev("Optional", at(10), at(12), busy=False)], at(0), at(23, 59))) == [
        ("Mon 09:00", "17:00")
    ]


def test_zero_length_events_block_nothing() -> None:
    assert spans(free([ev("Reminder", at(12), at(12))], at(0), at(23, 59))) == [
        ("Mon 09:00", "17:00")
    ]


# --- time zones ---------------------------------------------------------------------------


def test_events_in_other_timezones_are_normalised() -> None:
    utc = UTC
    # 00:00-01:00 UTC on Mon 9th is 10:00-11:00 in Brisbane (UTC+10)
    e = ev(
        "UTC meeting",
        datetime(2026, 11, 9, 0, 0, tzinfo=utc),
        datetime(2026, 11, 9, 1, 0, tzinfo=utc),
    )
    assert e.start == at(10) and e.end == at(11)
    assert spans(free([e], at(0), at(23, 59))) == [("Mon 09:00", "10:00"), ("Mon 11:00", "17:00")]


def test_sydney_daylight_saving_event_lands_at_the_right_brisbane_time() -> None:
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")  # AEDT = UTC+11 in November; Brisbane stays UTC+10
    e = ev(
        "Sydney call",
        datetime(2026, 11, 9, 11, 0, tzinfo=syd),
        datetime(2026, 11, 9, 12, 0, tzinfo=syd),
    )
    assert (e.start.hour, e.end.hour) == (10, 11)  # 11:00 AEDT is 10:00 in Brisbane
    assert spans(free([e], at(0), at(23, 59))) == [("Mon 09:00", "10:00"), ("Mon 11:00", "17:00")]


def test_search_range_given_in_another_timezone() -> None:
    start = datetime(2026, 11, 8, 22, 0, tzinfo=UTC)  # = Mon 08:00 Brisbane
    end = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)  # = Mon 18:00 Brisbane
    assert spans(find_free_slots([], start, end, 60, working_hours=WH)) == [("Mon 09:00", "17:00")]


def test_days_are_24_hours_no_dst() -> None:
    slots = find_free_slots([], at(0), at(0, day=7), 60, working_hours=None)
    assert len(slots) == 7 and all(s.minutes == 24 * 60 for s in slots)


# --- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"duration": 0}, "at least 1"),
        ({"buffer": -1}, "negative"),
        ({"end_before": True}, "after start"),
        ({"wh": (time(17), time(9))}, "end after they start"),
    ],
)
def test_validation(kwargs: dict, match: str) -> None:
    start, end = (at(12), at(10)) if kwargs.get("end_before") else (at(0), at(23))
    with pytest.raises(ValueError, match=match):
        find_free_slots(
            [],
            start,
            end,
            kwargs.get("duration", 30),
            buffer_minutes=kwargs.get("buffer", 0),
            working_hours=kwargs.get("wh", WH),
        )


def test_naive_datetimes_are_rejected_by_the_event_model() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ev("x", datetime(2026, 11, 9, 10), datetime(2026, 11, 9, 11))
    with pytest.raises(ValueError, match="before its start"):
        ev("x", at(11), at(10))


def test_input_events_are_not_mutated_and_order_does_not_matter() -> None:
    evs = [ev("B", at(13), at(14)), ev("A", at(10), at(11))]
    snapshot = list(evs)
    a = free(evs, at(0), at(23, 59))
    assert evs == snapshot and a == free(list(reversed(evs)), at(0), at(23, 59))


# --- conflicts ----------------------------------------------------------------------------


def test_conflicts_overlap_and_too_close() -> None:
    evs = [
        ev("Overlaps", at(10, 30), at(11, 30)),
        ev("Close", at(12, 10), at(13)),
        ev("Far", at(15), at(16)),
        ev("Free", at(10), at(12), busy=False),
        all_day("Holiday", 0),
    ]
    overlapping, close = conflicts_for(evs, at(11), at(12), buffer_minutes=15)
    assert [e.title for e in overlapping] == ["Overlaps"]
    assert [e.title for e in close] == ["Close"]


def test_touching_events_do_not_overlap() -> None:
    overlapping, close = conflicts_for([ev("A", at(10), at(11))], at(11), at(12))
    assert overlapping == [] and close == []
    _, close = conflicts_for([ev("A", at(10), at(11))], at(11), at(12), buffer_minutes=5)
    assert [e.title for e in close] == ["A"]  # touching is 'too close' once a buffer is wanted


def test_busy_intervals_merge() -> None:
    out = busy_intervals(
        [ev("A", at(10), at(11)), ev("B", at(11), at(12)), ev("C", at(14), at(15))]
    )
    assert [(i.start.hour, i.end.hour) for i in out] == [(10, 12), (14, 15)]
