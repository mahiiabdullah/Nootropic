"""Personal ranking: what matters to *you*, from what you follow and what you do.

Two signals:
- Explicit interests: topics, categories or sources you follow (+) or mute (hidden).
- Learned affinity: every open / save / "more like this" / "less like this" / hide
  nudges the topics, categories and source of that article up or down. Old
  signals fade (30-day half-life), so the ranking follows your interests as they change.

Score (0–100) = importance + personal interest + freshness, with plain-language
reasons ("You follow Rust", "You often read BleepingComputer").
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from neurovex.core.db import utcnow
from neurovex.modules.news.models import InteractionAction, InterestKind

ACTION_WEIGHTS: dict[str, float] = {
    InteractionAction.OPEN: 1.0,
    InteractionAction.SAVE: 2.0,
    InteractionAction.MORE: 3.0,
    InteractionAction.LESS: -3.0,
    InteractionAction.HIDE: -2.0,
}
HALF_LIFE_DAYS = 30.0
LEARN_WINDOW_DAYS = 180
LEARN_SCALE = 4.0  # sum of decayed weights that maps to ~0.76 affinity (tanh(1))
RECENCY_HALF_LIFE_H = 18.0
PENDING_IMPORTANCE_PRIOR = 4  # unsummarized articles rank as "ordinary" until scored

W_IMPORTANCE, W_INTEREST, W_RECENCY = 0.40, 0.40, 0.20


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def article_terms(
    topics: Iterable[str] | None, categories: Iterable[str] | None, source_id: int
) -> list[str]:
    """What an article is 'about', as display-cased terms like `topic:Rust`."""
    terms = [f"topic:{t}" for t in (topics or []) if t]
    terms += [f"cat:{c}" for c in (categories or []) if c]
    terms.append(f"src:{source_id}")
    return terms


def term_key(term: str) -> str:
    kind, _, value = term.partition(":")
    return f"{kind}:{norm(value)}"


@dataclass(frozen=True)
class InterestRule:
    id: int
    kind: str
    value: str  # display
    key: str  # normalized
    weight: float  # +1 follow, -1 mute


@dataclass
class Evaluation:
    score: float
    muted: bool
    followed: bool
    reasons: list[str]


@dataclass
class Profile:
    rules: list[InterestRule] = field(default_factory=list)
    learned: dict[str, float] = field(default_factory=dict)  # term key -> affinity (-1, 1)
    labels: dict[str, str] = field(default_factory=dict)  # term key -> display label
    source_names: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._follow_text = _text_matcher(
            r for r in self.rules if r.kind == InterestKind.TOPIC and r.weight > 0
        )
        self._mute_text = _text_matcher(
            r for r in self.rules if r.kind == InterestKind.TOPIC and r.weight < 0
        )

    @property
    def follows(self) -> list[InterestRule]:
        return [r for r in self.rules if r.weight > 0]

    @property
    def mutes(self) -> list[InterestRule]:
        return [r for r in self.rules if r.weight < 0]

    def is_empty(self) -> bool:
        return not self.rules and not self.learned

    def label(self, key: str) -> str:
        kind, _, value = key.partition(":")
        if kind == "src" and value.isdigit():
            return self.source_names.get(int(value), "this source")
        return self.labels.get(key, value)

    def top_learned(self, n: int = 8, positive: bool = True) -> list[tuple[str, str, float]]:
        items = [(k, a) for k, a in self.learned.items() if (a > 0) == positive]
        items.sort(key=lambda kv: abs(kv[1]), reverse=True)
        return [(k, self.label(k), round(a, 2)) for k, a in items[:n]]

    def _match(
        self,
        weight_sign: int,
        text_re: re.Pattern[str] | None,
        text: str,
        topic_keys: set[str],
        categories: set[str],
        source_id: int,
    ) -> InterestRule | None:
        for r in self.rules:
            if (r.weight > 0) != (weight_sign > 0):
                continue
            if r.kind == InterestKind.CATEGORY and r.key in categories:
                return r
            if r.kind == InterestKind.SOURCE and r.key == str(source_id):
                return r
            if r.kind == InterestKind.TOPIC and r.key in topic_keys:
                return r
        if text_re and (m := text_re.search(text)):
            hit = norm(m.group(0))
            return next(
                (
                    r
                    for r in self.rules
                    if r.kind == InterestKind.TOPIC
                    and r.key == hit
                    and (r.weight > 0) == (weight_sign > 0)
                ),
                None,
            )
        return None

    def evaluate(
        self,
        *,
        title: str,
        text: str | None,
        topics: list[str] | None,
        categories: list[str] | None,
        source_id: int,
        importance: int | None,
        when: datetime,
        now: datetime | None = None,
    ) -> Evaluation:
        now = now or utcnow()
        blob = f"{title}\n{text or ''}"[:4000]
        topic_keys = {norm(t) for t in topics or []}
        cats = {norm(c) for c in categories or []}

        mute = self._match(-1, self._mute_text, blob, topic_keys, cats, source_id)
        follow = self._match(+1, self._follow_text, blob, topic_keys, cats, source_id)

        terms = [term_key(t) for t in article_terms(topics, categories, source_id)]
        affs = [(k, self.learned[k]) for k in terms if k in self.learned]
        learned = max(-1.0, min(1.0, sum(a for _, a in affs)))

        imp = importance if importance is not None else PENDING_IMPORTANCE_PRIOR
        age_h = max(0.0, (now - when).total_seconds() / 3600)
        recency = 0.5 ** (age_h / RECENCY_HALF_LIFE_H)
        interest = (1.0 if follow else 0.0) + 0.8 * learned  # about [-0.8, 1.8]
        interest_n = max(0.0, min(1.0, (interest + 0.8) / 2.6))
        score = 100 * (W_IMPORTANCE * imp / 10 + W_INTEREST * interest_n + W_RECENCY * recency)

        reasons: list[str] = []
        if follow:
            reasons.append(f"You follow {follow.value}")
        for key, aff in sorted(affs, key=lambda kv: kv[1], reverse=True):
            if aff < 0.25 or len(reasons) >= 2:
                break
            reasons.append(_learned_reason(key, self.label(key)))
        if importance is not None and importance >= 8:
            reasons.append(f"High importance ({importance}/10)")
        if age_h < 2 and imp >= 6:
            reasons.append("Just in")

        return Evaluation(
            score=round(score, 1), muted=mute is not None, followed=follow is not None,
            reasons=reasons[:3],
        )  # fmt: skip


def _learned_reason(key: str, label: str) -> str:
    kind = key.partition(":")[0]
    if kind == "src":
        return f"You often read {label}"
    if kind == "cat":
        return f"You like {label} stories"
    return f"You often read about {label}"


def _text_matcher(rules: Iterable[InterestRule]) -> re.Pattern[str] | None:
    keys = sorted({r.key for r in rules if r.key}, key=len, reverse=True)
    if not keys:
        return None
    alternation = "|".join(re.escape(k) for k in keys)
    return re.compile(rf"(?<![\w]){alternation}(?![\w])", re.IGNORECASE)


def learn_affinities(
    interactions: Iterable[tuple[str, list[str], datetime]], now: datetime | None = None
) -> tuple[dict[str, float], dict[str, str]]:
    """Turn (action, terms, when) history into term affinities in (-1, 1)."""
    now = now or utcnow()
    totals: dict[str, float] = {}
    labels: dict[str, str] = {}
    for action, terms, when in interactions:
        w = ACTION_WEIGHTS.get(action, 0.0)
        if not w:
            continue
        age_days = max(0.0, (now - when).total_seconds() / 86400)
        decayed = w * 0.5 ** (age_days / HALF_LIFE_DAYS)
        for t in terms:
            k = term_key(t)
            totals[k] = totals.get(k, 0.0) + decayed
            labels.setdefault(k, t.partition(":")[2])
    affinities = {k: math.tanh(v / LEARN_SCALE) for k, v in totals.items()}
    return {k: a for k, a in affinities.items() if abs(a) >= 0.05}, labels


def learning_cutoff(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=LEARN_WINDOW_DAYS)
