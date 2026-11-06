"""Build the small `<memory>` block that goes in front of the model each turn.

Budgeted on purpose: standing instructions and core facts always, plus the few
memories that match what the user is talking about right now. A typical block is
100–400 tokens; it never exceeds MAX_CHARS (~500 tokens).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from neurovex.memory.models import Memory, MemoryKind
from neurovex.memory.store import MemoryStore

MAX_ITEMS = 14
MAX_CHARS = 1_800
CORE_ITEMS = 8
RELEVANT_ITEMS = 8


@dataclass
class Recall:
    block: str = ""
    ids: list[int] = field(default_factory=list)
    relevant_ids: list[int] = field(default_factory=list)


def _line(m: Memory) -> str:
    when = f" ({m.created_at.date().isoformat()})" if m.kind == MemoryKind.EVENT.value else ""
    return f"[m{m.id}] ({m.kind}) {m.text}{when}"


async def recall(store: MemoryStore, query: str, extra_query: str = "") -> Recall:
    """Core memories + those relevant to `query` (and the previous user message)."""
    core = await store.core(CORE_ITEMS)
    relevant = await store.search(f"{query} {extra_query}".strip(), RELEVANT_ITEMS)
    chosen: list[Memory] = []
    seen: set[int] = set()
    for m in [*core, *relevant]:
        if m.id not in seen and len(chosen) < MAX_ITEMS:
            chosen.append(m)
            seen.add(m.id)
    lines: list[str] = []
    used: list[int] = []
    size = 0
    for m in chosen:
        line = _line(m)
        if size + len(line) > MAX_CHARS:
            break
        lines.append(line)
        used.append(m.id)
        size += len(line) + 1
    if not lines:
        return Recall()
    relevant_ids = [m.id for m in relevant if m.id in used]
    return Recall(block="<memory>\n" + "\n".join(lines) + "\n</memory>", ids=used,
                  relevant_ids=relevant_ids)  # fmt: skip
