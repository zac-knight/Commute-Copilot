"""User profile: home stop, usual destinations, timezone. Configurable, never hard-coded in
agent logic. Loaded from ``data/profile.json`` (git-ignored); defaults are public landmarks,
not a home address."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    home: str = "North Lakes station"  # a stop/station name or stop_id
    destinations: dict[str, str] = Field(
        default_factory=lambda: {
            "chermside": "Chermside bus station",
            "southbank": "South Bank station",
            "city": "Central station",
        }
    )
    timezone: str = "Australia/Brisbane"
    # Scheduling preferences (used by the calendar agent).
    working_hours_start: str = "09:00"
    working_hours_end: str = "17:00"
    buffer_minutes: int = 10  # clear time kept around meetings
    # Weather location: North Lakes suburb centre (a public landmark, not a home address).
    latitude: float = -27.235
    longitude: float = 153.020

    def resolve_alias(self, text: str) -> str:
        """'home' / 'my stop' -> the home place; a destination nickname -> its place; anything
        else is returned unchanged."""
        key = text.strip().lower()
        if key in {"home", "my home", "my stop", "here"}:
            return self.home
        return self.destinations.get(key, text)


def save_profile(path: Path, profile: UserProfile) -> None:
    """Atomic write (temp file + rename) of the profile as JSON."""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_profile(path: Path) -> UserProfile:
    if path.exists():
        return UserProfile.model_validate(json.loads(path.read_text()))
    return UserProfile()
