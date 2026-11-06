"""Memory storage, search and recall: all local, no AI calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import case, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from neurovex.core.db import utcnow
from neurovex.memory.models import KINDS, Memory, MemoryKind

MAX_TEXT = 300
DUPLICATE_SIMILARITY = 0.75  # word-set overlap above which a new memory updates an old one

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can", "could", "did",
    "do", "does", "for", "from", "had", "has", "have", "he", "her", "his", "how", "i", "if",
    "in", "into", "is", "it", "its", "just", "me", "my", "of", "on", "or", "our", "she", "so",
    "than", "that", "the", "their", "them", "then", "there", "these", "they", "this", "to",
    "too", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "you", "your", "about", "any", "anything", "know", "tell", "remember", "please",
    "also", "some", "more", "most", "very", "am", "im", "i'm", "i've", "i'd", "don't", "dont",
})  # fmt: skip
# Never store credentials: keys, tokens, passwords, card-like numbers.
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|pk_(live|test)_[A-Za-z0-9_\-]{8,}|gsk_[A-Za-z0-9]{12,}|"
    r"AKIA[0-9A-Z]{12,}|ghp_[A-Za-z0-9]{20,}|xox[bp]-[A-Za-z0-9-]{10,}|"
    r"\b(password|passcode|passwd|pin code|api key|secret key)\b\s*(is|:|=)|"
    r"\b(?:\d[ -]?){13,19}\b)",
    re.IGNORECASE,
)


def clean(text_: str) -> str:
    return re.sub(r"\s+", " ", text_ or "").strip()[:MAX_TEXT]


def words(text_: str) -> list[str]:
    seen: dict[str, None] = {}
    for w in re.findall(r"[\w][\w'+#.-]*", text_.lower()):
        w = w.strip(".'-")
        if len(w) >= 2 and w not in _STOPWORDS:
            seen.setdefault(w, None)
    return list(seen)


def looks_secret(text_: str) -> bool:
    return bool(_SECRET.search(text_))


def similarity(a: str, b: str) -> float:
    sa, sb = set(words(a)), set(words(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def fts_query(text_: str, limit_words: int = 12) -> str | None:
    """User text → FTS5 query: OR of quoted prefix terms (safe against FTS syntax)."""
    terms = [w.replace('"', "") for w in words(text_)][:limit_words]
    return " OR ".join(f'"{t}"*' for t in terms if t) or None


@dataclass
class ConversationHit:
    conversation_id: int
    title: str
    role: str
    snippet: str
    at: datetime


class MemoryStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    # --- write -------------------------------------------------------------------------

    async def add(
        self,
        text_: str,
        kind: str = MemoryKind.FACT,
        importance: int = 2,
        *,
        pinned: bool = False,
        source: str = "chat",
        conversation_id: int | None = None,
    ) -> tuple[Memory | None, str]:
        """Store a memory. Returns (memory, "added" | "merged" | reason it was rejected)."""
        body = clean(text_)
        if len(body) < 3:
            return None, "empty"
        if looks_secret(body):
            return None, "looks like a secret; not stored"
        kind = kind if kind in KINDS else MemoryKind.FACT
        importance = max(1, min(3, int(importance or 2)))
        async with self.sessions() as s:
            # Near-duplicate of something we already know? Refresh it instead of adding.
            for existing, _ in await self._search(s, body, limit=5):
                if similarity(existing.text, body) >= DUPLICATE_SIMILARITY:
                    existing.text = body
                    existing.importance = max(existing.importance, importance)
                    existing.updated_at = utcnow()
                    await s.commit()
                    return existing, "merged"
            m = Memory(
                kind=kind, text=body, importance=importance, pinned=pinned, source=source,
                conversation_id=conversation_id,
            )  # fmt: skip
            s.add(m)
            await s.commit()
            return m, "added"

    async def update(
        self,
        memory_id: int,
        *,
        text_: str | None = None,
        kind: str | None = None,
        importance: int | None = None,
        pinned: bool | None = None,
    ) -> Memory | None:
        async with self.sessions() as s:
            m = await s.get(Memory, memory_id)
            if m is None:
                return None
            if text_ is not None:
                body = clean(text_)
                if len(body) < 3 or looks_secret(body):
                    return None
                m.text = body
            if kind is not None and kind in KINDS:
                m.kind = kind
            if importance is not None:
                m.importance = max(1, min(3, int(importance)))
            if pinned is not None:
                m.pinned = pinned
            m.updated_at = utcnow()
            await s.commit()
            return m

    async def forget(self, memory_id: int) -> Memory | None:
        async with self.sessions() as s:
            m = await s.get(Memory, memory_id)
            if m is None:
                return None
            await s.delete(m)
            await s.commit()
            return m

    async def clear(self) -> int:
        async with self.sessions() as s:
            result = await s.execute(delete(Memory))
            await s.commit()
            return result.rowcount or 0

    async def mark_used(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self.sessions() as s:
            await s.execute(
                update(Memory)
                .where(Memory.id.in_(ids))
                .values(last_used_at=utcnow(), use_count=Memory.use_count + 1)
            )
            await s.commit()

    # --- read --------------------------------------------------------------------------

    async def get(self, memory_id: int) -> Memory | None:
        async with self.sessions() as s:
            return await s.get(Memory, memory_id)

    async def list(self, kind: str | None = None, limit: int = 500) -> list[Memory]:
        stmt = select(Memory)
        if kind:
            stmt = stmt.where(Memory.kind == kind)
        stmt = stmt.order_by(
            Memory.pinned.desc(), Memory.importance.desc(), Memory.updated_at.desc()
        ).limit(limit)
        async with self.sessions() as s:
            return list(await s.scalars(stmt))

    async def count(self) -> int:
        async with self.sessions() as s:
            return await s.scalar(select(func.count()).select_from(Memory)) or 0

    async def search(self, query: str, limit: int = 8) -> list[Memory]:
        async with self.sessions() as s:
            return [m for m, _ in await self._search(s, query, limit)]

    async def _search(self, s: AsyncSession, query: str, limit: int) -> list[tuple[Memory, float]]:
        q = fts_query(query)
        if q is None:
            return []
        rows = (
            await s.execute(
                text(
                    "SELECT rowid, bm25(memories_fts) AS rank FROM memories_fts "
                    "WHERE memories_fts MATCH :q ORDER BY rank LIMIT :n"
                ),
                {"q": q, "n": limit * 3},
            )
        ).all()
        if not rows:
            return []
        ranks = {rid: rank for rid, rank in rows}
        found = {m.id: m for m in await s.scalars(select(Memory).where(Memory.id.in_(ranks)))}
        # BM25 is negative (lower = better); nudge by importance so core facts win ties.
        scored = sorted(
            ((found[i], ranks[i] - 0.5 * found[i].importance) for i in ranks if i in found),
            key=lambda p: p[1],
        )
        return scored[:limit]

    async def core(self, limit: int = 8) -> list[Memory]:
        """Always-relevant memories: how to answer (instructions, preferences), pinned, then
        the most important facts. Preferences ("prefers short answers") apply to every
        conversation, so they can't depend on keyword matches."""
        stmt = (
            select(Memory)
            .where(
                Memory.kind.in_([MemoryKind.INSTRUCTION.value, MemoryKind.PREFERENCE.value])
                | Memory.pinned.is_(True)
                | (Memory.importance >= 3)
            )
            .order_by(
                case(
                    (Memory.kind == MemoryKind.INSTRUCTION.value, 0),
                    (Memory.kind == MemoryKind.PREFERENCE.value, 1),
                    else_=2,
                ),
                Memory.pinned.desc(),
                Memory.importance.desc(),
                Memory.updated_at.desc(),
            )
            .limit(limit)
        )
        async with self.sessions() as s:
            return list(await s.scalars(stmt))

    async def search_conversations(self, query: str, limit: int = 6) -> list[ConversationHit]:
        """Past chat messages matching `query` (episodic recall without summarizing)."""
        q = fts_query(query)
        if q is None:
            return []
        async with self.sessions() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT m.conversation_id, c.title, m.role, "
                        "snippet(chat_messages_fts, 0, '«', '»', ' … ', 24), m.created_at "
                        "FROM chat_messages_fts "
                        "JOIN chat_messages m ON m.id = chat_messages_fts.rowid "
                        "JOIN chat_conversations c ON c.id = m.conversation_id "
                        "WHERE chat_messages_fts MATCH :q ORDER BY bm25(chat_messages_fts) LIMIT :n"
                    ),
                    {"q": q, "n": limit},
                )
            ).all()
        hits = []
        for cid, title, role, snip, at in rows:
            when = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
            if when.tzinfo is None:  # stored as naive UTC (see core.db.UTCDateTime)
                when = when.replace(tzinfo=UTC)
            hits.append(ConversationHit(cid, title, role, snip, when))
        return hits
