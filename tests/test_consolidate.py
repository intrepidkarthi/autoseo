"""What may be folded into what — the most destructive decision the loop makes unattended.

Every case here is shaped like a real pair from the September 2026 audit, because the audit found
both failure directions live at once: three overwhelming duplicates sitting below the five-query
bar (splitting the voice cluster for weeks), and the machinery blind to redirects the site already
had (GSC reporting ghosts, the ledger remembering only its own merges).
"""
from __future__ import annotations

import datetime as dt

import pytest

from autoseo.decide import consolidate

SITE = "https://getdailyvox.com"
DATE = (dt.date.today() - dt.timedelta(days=5)).isoformat()


def seed(conn, page, *, imp, clicks=0, pos, queries=(), indexed=True):
    url = f"{SITE}/blog/{page}"
    conn.execute(
        "INSERT INTO gsc_page_daily (date, page, clicks, impressions, position) VALUES (?,?,?,?,?)",
        (DATE, url, clicks, imp, pos),
    )
    for q in queries:
        conn.execute(
            "INSERT INTO gsc_page_query (date, page, query, impressions, position) VALUES (?,?,?,1,?)",
            (DATE, url, q, pos),
        )
    conn.execute(
        "INSERT INTO url_index_status (url, checked_at, indexed) VALUES (?,?,?)",
        (url, DATE, int(indexed)),
    )
    return url


@pytest.fixture
def live(monkeypatch):
    """Every page serves 200 unless a test says otherwise."""
    down: set[str] = set()
    monkeypatch.setattr(consolidate, "_serves_200", lambda url: url not in down)
    return down


def pair(db, live, *, shared=4, w_imp=2100, l_imp=34, l_clicks=0, w_pos=16.2, l_pos=29.2,
         w_indexed=True, l_indexed=True):
    """A winner/loser pair shaped like best-voice-journal-app vs its stragglers."""
    from autoseo.core.db import session

    qs = [f"query {i}" for i in range(shared)]
    with session() as conn:
        winner = seed(conn, "a-winner", imp=w_imp, pos=w_pos, queries=qs, indexed=w_indexed)
        loser = seed(conn, "b-loser", imp=l_imp, clicks=l_clicks, pos=l_pos, queries=qs,
                 indexed=l_indexed)
        conn.commit()
    return winner, loser


# --- the two tiers ------------------------------------------------------------------------------

def test_ordinary_tier_five_shared_modest_ratio(db, live):
    pair(db, live, shared=5, w_imp=900, l_imp=560, l_clicks=0)     # ratio 1.6
    assert len(consolidate.candidates()) == 1


def test_dominant_tier_thin_overlap_overwhelming_ratio(db, live):
    """The what-is-the-best-voice-diary-app case: four shared queries, outdrawn 62:1.
    Under the five-query bar alone this sat splitting the voice cluster for weeks."""
    pair(db, live, shared=4, w_imp=2100, l_imp=34)                  # ratio 62
    assert len(consolidate.candidates()) == 1


def test_dominant_tier_tolerates_one_noise_click(db, live):
    """The voice-diary-app case: one click in ninety days is noise, not conversion."""
    pair(db, live, shared=3, w_imp=2100, l_imp=53, l_clicks=1)      # ratio 40
    assert len(consolidate.candidates()) == 1


def test_one_click_still_blocks_the_ordinary_tier(db, live):
    pair(db, live, shared=6, w_imp=900, l_imp=560, l_clicks=1)      # ratio 1.6
    assert consolidate.candidates() == []


def test_thin_overlap_needs_the_overwhelming_ratio(db, live):
    pair(db, live, shared=3, w_imp=300, l_imp=60)                   # ratio 5 — between the tiers
    assert consolidate.candidates() == []


def test_one_shared_query_never_qualifies(db, live):
    """voice-to-text-journal: one shared query at ratio 132 — and a page-two ranking of its own
    on a distinct intent. Whatever the ratio says, one query is coincidence, not duplication."""
    pair(db, live, shared=1, w_imp=2100, l_imp=16)                  # ratio 131
    assert consolidate.candidates() == []


# --- the guards both tiers share ----------------------------------------------------------------

def test_winner_must_be_better_on_position_too(db, live):
    pair(db, live, shared=5, w_imp=900, l_imp=60, w_pos=30.0, l_pos=14.0)
    assert consolidate.candidates() == []


def test_unindexed_loser_blocks(db, live):
    """best-free-journal-app-2026: stronger on every metric and dropped from the index —
    its numbers describe a state that no longer exists."""
    pair(db, live, l_indexed=False)
    assert consolidate.candidates() == []


def test_loser_already_redirecting_is_left_alone(db, live):
    """A hand-made redirect lives only in vercel.json, which this half cannot read, and GSC keeps
    reporting the URL for weeks. The live check is the only thing standing between that residue
    and a second redirect for a page that already has one."""
    winner, loser = pair(db, live)
    live.add(loser)
    assert consolidate.candidates() == []


def test_winner_that_redirects_would_chain_or_loop(db, live):
    """The hand-repaired AI pair, exactly: GSC scores the redirected URL as the stronger page, so
    without this check the loop would propose folding the live page into the ghost — whose
    redirect points back at the live page. A permanent redirect cycle, shipped unattended."""
    winner, loser = pair(db, live)
    live.add(winner)
    assert consolidate.candidates() == []
