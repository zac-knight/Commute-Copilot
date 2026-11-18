"""Delimiting for untrusted external text (calendar titles/descriptions, transit alerts, ...).

This is a *mitigation*, not a defence: a model can still be talked into following text inside the
delimiters. The real protection is structural: agents cannot write anywhere without a human
approval (see core/approvals.py and docs/safety.md). The wrapper's job is to (a) make it
unmistakable to the model which text is data, and (b) stop the text from forging its own closing
delimiter and escaping the block.
"""

from __future__ import annotations

import re

TAG = "untrusted_data"
# Any '<' (optionally followed by '/' and spaces) then 'untrusted', closed or not: attackers can
# leave the tag unterminated or glue letters to it, so no word-boundary is required.
_FORGED_TAG = re.compile(r"<\s*/?\s*untrusted[^>]*>?", re.IGNORECASE)
# C0/C1 controls, zero-width and bidi-control characters, line/paragraph separators.
_INVISIBLE_RANGES = [
    (0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), (0x7F, 0x9F), (0x200B, 0x200F),
    (0x2028, 0x202E), (0x2060, 0x2069), (0xFEFF, 0xFEFF),
]  # fmt: skip
_CONTROL = re.compile(
    "[" + "".join(f"\\u{lo:04x}-\\u{hi:04x}" for lo, hi in _INVISIBLE_RANGES) + "]"
)
_SOURCE = re.compile(r"[^A-Za-z0-9_.:-]")


def sanitise(text: str) -> str:
    """Remove forged delimiter tags, control characters and invisible direction/zero-width
    characters (used to hide or reorder text)."""
    return _CONTROL.sub("", _FORGED_TAG.sub("[tag removed]", text))


def wrap_untrusted(text: str | None, source: str, *, max_chars: int = 500) -> str:
    """``<untrusted_data source="...">text</untrusted_data>``, sanitised and length-capped."""
    clean = sanitise(text or "")
    if len(clean) > max_chars:
        clean = f"{clean[:max_chars]}…[+{len(clean) - max_chars} chars]"
    return f'<{TAG} source="{_SOURCE.sub("_", source)[:40]}">{clean}</{TAG}>'


def unwrap_untrusted(wrapped: str) -> str:
    """Inverse of wrap_untrusted for display purposes (UI/CLI). Not security relevant."""
    m = re.fullmatch(rf'<{TAG} source="[^"]*">(.*)</{TAG}>', wrapped, re.DOTALL)
    return m.group(1) if m else wrapped
