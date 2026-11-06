from __future__ import annotations

from datetime import timedelta

from neurovex.core.db import utcnow
from neurovex.modules.news.models import InterestKind
from neurovex.modules.news.personalize import (
    InterestRule,
    Profile,
    article_terms,
    learn_affinities,
    norm,
)

NOW = utcnow()


def rule(kind, value, weight=1.0, i=1):
    key = value if kind == InterestKind.SOURCE else norm(value)
    return InterestRule(id=i, kind=kind, value=value, key=key, weight=weight)


def ev(profile, *, title="Something happened", text="", topics=None, categories=None,
       source_id=1, importance=5, hours_old=1.0):  # fmt: skip
    return profile.evaluate(
        title=title,
        text=text,
        topics=topics,
        categories=categories,
        source_id=source_id,
        importance=importance,
        when=NOW - timedelta(hours=hours_old),
        now=NOW,
    )


def test_neutral_profile_ranks_by_importance_then_freshness():
    p = Profile()
    assert ev(p, importance=9).score > ev(p, importance=5).score
    assert ev(p, hours_old=1).score > ev(p, hours_old=30).score
    assert ev(p).reasons == [] and not ev(p).followed


def test_followed_topic_matches_topics_and_text_with_word_boundaries():
    p = Profile(rules=[rule(InterestKind.TOPIC, "Rust")])
    assert ev(p, topics=["Rust"]).followed
    assert ev(p, title="What's new in rust 1.90").followed
    assert not ev(p, title="Why trust matters in security").followed  # not 'rust' inside 'trust'
    hit = ev(p, topics=["Rust"], importance=5)
    assert hit.score > ev(Profile(), importance=5).score
    assert hit.reasons[0] == "You follow Rust"


def test_follow_beats_slightly_more_important_but_irrelevant_news():
    p = Profile(rules=[rule(InterestKind.TOPIC, "Rust")])
    assert ev(p, topics=["Rust"], importance=5).score > ev(p, importance=7).score
    # ...but a critical story everyone needs still wins.
    assert ev(p, importance=10).score > ev(p, topics=["Rust"], importance=4).score


def test_category_and_source_follows():
    p = Profile(rules=[rule(InterestKind.CATEGORY, "AI"), rule(InterestKind.SOURCE, "7", i=2)])
    assert ev(p, categories=["AI"]).followed
    assert ev(p, source_id=7).followed
    assert not ev(p, categories=["Hardware"], source_id=3).followed


def test_mutes():
    p = Profile(
        rules=[rule(InterestKind.TOPIC, "crypto", -1.0), rule(InterestKind.SOURCE, "9", -1.0, 2)]
    )
    assert ev(p, title="Crypto exchange collapses").muted
    assert ev(p, source_id=9).muted
    assert not ev(p, title="Cryptography paper").muted  # whole word only


def test_learning_from_interactions():
    history = [
        ("more", article_terms(["Rust"], ["Programming"], 3), NOW - timedelta(days=1)),
        ("save", article_terms(["Rust"], ["Programming"], 3), NOW - timedelta(days=2)),
        ("less", article_terms(["Crypto"], ["Business"], 4), NOW - timedelta(days=1)),
    ]
    learned, labels = learn_affinities(history, now=NOW)
    assert learned["topic:rust"] > 0.5 and learned["topic:crypto"] < -0.4
    assert labels["topic:rust"] == "Rust"

    p = Profile(learned=learned, labels=labels, source_names={3: "Rust Blog"})
    liked = ev(p, topics=["Rust"], categories=["Programming"], source_id=3)
    disliked = ev(p, topics=["Crypto"], categories=["Business"], source_id=4)
    assert liked.score > ev(p).score > disliked.score
    assert any("Rust" in r for r in liked.reasons)
    assert [label for _, label, _ in p.top_learned(3)][0] in {"Rust", "Programming", "Rust Blog"}


def test_old_signals_fade():
    terms = article_terms(["Rust"], [], 1)
    fresh, _ = learn_affinities([("more", terms, NOW)], now=NOW)
    old, _ = learn_affinities([("more", terms, NOW - timedelta(days=90))], now=NOW)
    assert fresh["topic:rust"] > old.get("topic:rust", 0) * 4


def test_pending_articles_get_a_neutral_prior():
    p = Profile()
    pending = ev(p, importance=None)
    assert ev(p, importance=3).score < pending.score < ev(p, importance=6).score
