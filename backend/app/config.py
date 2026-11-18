"""Settings and model choices.

This is the ONLY module that names a model. Everything else asks for a role
(``ROLES["router"]``) so a model swap is a one-line change here.

All inference is local (Ollama): no API keys, no per-token cost. The cost metrics that matter
are therefore tokens and latency, both recorded on every span.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class RoleConfig(BaseModel, frozen=True):
    """Which Ollama model a role uses, and whether the model may 'think' first.

    Thinking models (qwen3) are much slower with thinking on; it is off for routing and
    tool-calling steps and on only where reasoning quality matters (synthesis).
    """

    model: str
    think: bool


# Tags are Ollama library names. UNVERIFIED until pulled and called (see docs/decisions.md).
ROLES: dict[str, RoleConfig] = {
    "router": RoleConfig(model="qwen3:8b", think=False),
    "agent": RoleConfig(model="qwen3:8b", think=False),
    # qwen3:14b (~9 GB) was the plan for synthesis but is not installed; the 8B does both jobs until
    # the owner chooses to pull it (the Phase 7 model comparison needs a second model anyway).
    "synthesis": RoleConfig(model="qwen3:8b", think=False),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    demo: bool = False
    demo_speed: float = 1.0  # replay speed multiplier for demo mode (0 = instant)

    db_path: Path = BACKEND_DIR / "data" / "commute.sqlite"
    analytics_db_path: Path = BACKEND_DIR / "data" / "analytics.sqlite"  # polled delays (Phase 8)
    frontend_origin: str = "http://localhost:5173"

    # Transit (TransLink SEQ open data: public, no API key).
    gtfs_auto_refresh: bool = True  # check for a new static feed at start-up and then daily
    gtfs_refresh_hours: float = 24.0
    gtfs_dir: Path = BACKEND_DIR / "data" / "gtfs"
    profile_path: Path = BACKEND_DIR / "data" / "profile.json"
    ui_settings_path: Path = BACKEND_DIR / "data" / "ui_settings.json"  # router model choice
    walk_speed_m_per_min: float = 80.0
    transfer_radius_m: float = 250.0
    min_transfer_s: int = 60
    rt_ttl_seconds: float = 25.0
    on_time_threshold_s: int = 60

    # Calendar: local calendar.json + read-only imports from import/*.ics (git-ignored).
    calendar_dir: Path = BACKEND_DIR / "data" / "calendar"
    approval_ttl_minutes: int = 30
    max_pending_actions: int = 10

    # Weather (Open-Meteo: free, no key, non-commercial; 600 calls/min limit).
    weather_ttl_seconds: float = 600.0
    weather_max_stale_seconds: float = 3600.0

    # Ollama. The first call after a cold start loads the model into memory (slow).
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: float = 180.0
    num_ctx: int = 8192

    # Agent loop and per-request budget.
    max_agent_steps: int = 6
    max_request_tokens: int = 100_000
    max_request_seconds: float = 120.0
    tool_result_max_chars: int = 20_000

    # Tracing.
    trace_preview_chars: int = 2000  # the trace viewer shows these (truncated, with a marker)

    def secret_values(self) -> list[str]:
        """Secret strings that must never appear in logs or traces (none yet: no API keys;
        Google OAuth tokens are caught by pattern in tracing.redact)."""
        return []


@lru_cache
def get_settings() -> Settings:
    return Settings()
