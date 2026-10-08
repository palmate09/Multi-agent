"""Parsing of LLM output into runnable files.

Models wrap code in markdown fences and sometimes emit a single unnamed fence.
Writing that verbatim produces files that cannot be parsed, which shows up as a
pytest collection error rather than an obvious "bad generation".
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```[^\n`]*\n(.*?)(?:\n```|\Z)", re.S)
_NAMED_BLOCK = re.compile(r"###\s+(\S+)\s*\n(.*?)(?=###\s+\S+\s*\n|\Z)", re.S)


def strip_fence(text: str) -> str:
    """Return the inner body of a fenced block, or the text unchanged."""
    matches = _FENCE.findall(text)
    if matches:
        return "\n\n".join(m.rstrip() for m in matches).strip()
    return text.strip()


def parse_file_blocks(text: str) -> dict[str, str]:
    """Parse ``### path`` blocks into ``{path: content}`` with fences removed.

    Falls back to a single unnamed fenced block, which is how the Tester emits
    its one test file.
    """
    files: dict[str, str] = {}
    for name, body in _NAMED_BLOCK.findall(text):
        name = name.strip().strip("`*:")
        if name:
            files[name] = strip_fence(body)
    if files:
        return files
    body = strip_fence(text)
    return {"test_tasks.py": body} if body else {}
