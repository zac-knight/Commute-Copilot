from __future__ import annotations

from pathlib import Path

from app.config import ROLES


def test_roles_defined() -> None:
    assert {"router", "agent", "synthesis"} <= set(ROLES)


def test_model_names_live_only_in_config() -> None:
    root = Path(__file__).resolve().parents[1]
    models = {r.model for r in ROLES.values()}
    offenders = [
        str(p.relative_to(root))
        for folder in ("app", "scripts")
        for p in (root / folder).rglob("*.py")
        if p.name != "config.py" and any(m in p.read_text() for m in models)
    ]
    assert offenders == []
