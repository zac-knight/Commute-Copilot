from __future__ import annotations

import pytest

from app.core.tracing import Tracer, TraceStore


@pytest.fixture
def store() -> TraceStore:
    return TraceStore(":memory:")


@pytest.fixture
def tracer(store: TraceStore) -> Tracer:
    return Tracer(store, preview_chars=200, secrets=["s3cret-value-123"])
