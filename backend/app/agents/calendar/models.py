"""Calendar data models."""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.timeutil import TZ


class CalEvent(BaseModel):
    """One calendar occurrence (recurring events are expanded into occurrences).

    ``busy``: whether it blocks time for scheduling. ICS TRANSP decides when present; otherwise
    timed events are busy and all-day events are not (Google/Apple treat all-day items such as
    birthdays and holidays as 'free' by default).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    busy: bool = True
    location: str = ""
    description: str = ""
    source: str = "local"  # "local" or "ics:<filename>"
    writable: bool = True  # only local events can be changed

    @field_validator("start", "end")
    @classmethod
    def _aware_local(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return v.astimezone(TZ)

    @model_validator(mode="after")
    def _order(self) -> CalEvent:
        if self.end < self.start:
            raise ValueError("event end is before its start")
        return self

    @property
    def etag(self) -> str:
        """Content hash: lets an approved update/delete detect that the event changed since the
        user reviewed it."""
        raw = "\x1f".join(
            [self.id, self.title, self.start.isoformat(), self.end.isoformat(),
             str(self.all_day), str(self.busy), self.location, self.description]
        )  # fmt: skip
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end
