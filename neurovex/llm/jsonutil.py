"""Getting one JSON object out of model text, however the model wrapped it."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json|JSON)?\s*|\s*```$")


def schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "\n\n## Output format\nRespond with only one JSON object (no prose, no markdown "
        "fences) that matches this JSON Schema exactly:\n"
        + json.dumps(schema, separators=(",", ":"))
    )


def extract_json(text: str) -> str:
    """Return the first complete JSON object in `text`, or raise ValueError."""
    t = _FENCE.sub("", text.strip()).strip()
    if t.startswith("{"):
        try:
            json.loads(t)
            return t
        except json.JSONDecodeError:
            pass
    start = t.find("{")
    while start != -1:
        end = _matching_brace(t, start)
        if end is not None:
            candidate = t[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        start = t.find("{", start + 1)
    raise ValueError("no JSON object found in model output")


def _matching_brace(t: str, start: int) -> int | None:
    depth, in_str, escape = 0, False, False
    for i in range(start, len(t)):
        c = t[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None
