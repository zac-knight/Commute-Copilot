from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.orchestrator_fixtures import build_test_state


def test_health_and_cors_only_for_the_local_frontend(tmp_path: Path) -> None:
    state, _ = build_test_state(tmp_path)
    c = TestClient(create_app(state))
    body = c.get("/health").json()
    assert body["status"] == "ok" and body["llm"]["models"]["router"]["model"]
    ok = c.get("/health", headers={"Origin": "http://localhost:5173"})
    bad = c.get("/health", headers={"Origin": "http://evil.example"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in bad.headers
