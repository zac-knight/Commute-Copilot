"""Versioned prompts live as files in ``orchestrator/prompts/<name>_v<N>.md``; the version is logged
on the trace spans of every call that uses them."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


def load_prompt(name: str, version: int) -> Prompt:
    path = PROMPT_DIR / f"{name}_v{version}.md"
    return Prompt(name, f"{name}-v{version}", path.read_text(encoding="utf-8").strip())
