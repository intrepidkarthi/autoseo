"""What stops the loop writing a page that competes with one of our own — and what must not.

The guard this pins had no position bound, and on 2026-08-30 that quietly stopped the loop: it
rejected all 24 candidates, several because a page of ours sat at position 85 for the query. Three
days passed with no article while every job reported success. Both halves are tested here, because
the failure was not the guard blocking too little — it was the guard blocking everything.
"""
from __future__ import annotations

import pytest

from autoseo.act import onpage
from autoseo.act.plan import _would_cannibalise

SITE = "https://getdailyvox.com"


def page(path: str, imp: float, pos: float) -> tuple[str, float, float]:
    return (f"{SITE}{path}", imp, pos)


# --- what must still be blocked ------------------------------------------------------------------

def test_blocks_when_our_page_is_close_enough_to_edit():
    ours = [page("/blog/best-voice-journal-app", 53, 12.0)]
    assert "edit it instead" in _would_cannibalise(ours, set())


def test_blocks_at_the_fixer_boundary():
    """Position 40 is the last position an edit is expected to move. It belongs to the fixer."""
    ours = [page("/blog/best-voice-journal-app", 53, onpage.FAQ_MAX_POSITION)]
    assert _would_cannibalise(ours, set()) is not None


def test_blocks_an_existing_pile_up():
    """The original bug: /blog/voice-journaling-app went out as the third page for one query."""
    ours = [page("/blog/best-voice-journal-app", 53, 62.0),
            page("/blog/voice-recorder-diary-app", 40, 68.0)]
    assert "already compete" in _would_cannibalise(ours, set())


def test_blocks_a_second_page_for_an_already_covered_incumbent():
    """'travel journal app', 'travel diary app' and 'trip journal app' are one article, not three."""
    ours = [page("/blog/best-journal-app-for-travelers", 61, 48.2)]
    assert "already covers" in _would_cannibalise(ours, {f"{SITE}/blog/best-journal-app-for-travelers"})


def test_the_cover_check_spans_runs_not_just_this_one():
    """The gap that survived the first fix. `claimed` was a per-run set, so the loop published
    /blog/best-diary-app-iphone on 2026-09-02 to relieve /blog/best-journal-app-iphone and had
    'journaling apps for ios' — same incumbent — at the top of the queue the next morning with
    nothing left to stop it. The set is seeded from the ledger now; this pins that it is consulted
    at all, which is the part a per-run set got wrong."""
    incumbent = f"{SITE}/blog/best-journal-app-iphone"
    ours = [page("/blog/best-journal-app-iphone", 51, 42.9)]
    assert _would_cannibalise(ours, set()) is None              # nothing published for it yet
    assert _would_cannibalise(ours, {incumbent}) is not None    # yesterday's post covered it


# --- what must get through -----------------------------------------------------------------------

def test_allows_when_nothing_of_ours_ranks():
    assert _would_cannibalise([], set()) is None


@pytest.mark.parametrize("pos", [40.1, 48.1, 59.7, 81.0, 85.7])
def test_allows_when_our_only_page_is_past_the_fixer(pos):
    """The regression. Every one of these was refused, and the brief had asked for each by name:
    it files them under `too-far` — 'needs a dedicated page'."""
    ours = [page("/blog/best-journal-app-iphone", 67, pos)]
    assert _would_cannibalise(ours, set()) is None


def test_a_claimed_incumbent_does_not_block_a_different_incumbent():
    ours = [page("/blog/best-journal-app-for-privacy", 38, 47.6)]
    assert _would_cannibalise(ours, {f"{SITE}/blog/best-journal-app-for-travelers"}) is None


def test_a_distant_pile_up_is_blocked_as_a_pile_up_not_as_an_editable_page():
    """`ours` is ordered by impressions, not position, so the top row need not be the best-ranked.
    The reason matters as much as the verdict: reported as "edit it instead", this query would be
    handed to the on-page fixer, which cannot take it — every page is past the fixer's range. It is
    a pile-up, and naming it one is what keeps the two arms from pointing at each other."""
    ours = [page("/blog/loud-but-distant", 90, 55.0), page("/blog/quieter-and-further", 3, 62.0)]
    assert _would_cannibalise(ours, set()) == "2 of our pages already compete for it"


# --- the ledger side: what makes the check span runs ----------------------------------------------

def test_incumbent_round_trips_through_the_ledger(db):
    """A planned post records the page it relieves, and the next run reads it back.

    Both halves matter and only the pair is useful: recording an incumbent nothing reads would not
    have stopped the third iPhone page, and reading a field nothing writes would return an empty
    set every morning — which is exactly what a per-run set already did.
    """
    from autoseo.act import ledger

    assert ledger.incumbents_supported(days=30) == set()

    ledger.plan(ledger.Item(
        kind=ledger.Kind.POST, title="Best Diary App for iPhone", body="...",
        rationale="...", meta={"slug": "best-diary-app-iphone",
                               "incumbent": f"{SITE}/blog/best-journal-app-iphone"},
    ))
    assert ledger.incumbents_supported(days=30) == {f"{SITE}/blog/best-journal-app-iphone"}


def test_a_dropped_post_releases_its_incumbent(db):
    """Planned and shipped hold the claim; a draft that never ran should not hold it forever."""
    from autoseo.act import ledger

    item_id = ledger.plan(ledger.Item(
        kind=ledger.Kind.POST, title="Best Diary App for iPhone", body="...",
        rationale="...", meta={"slug": "best-diary-app-iphone",
                               "incumbent": f"{SITE}/blog/best-journal-app-iphone"},
    ))
    ledger.drop(item_id, "superseded")
    assert ledger.incumbents_supported(days=30) == set()
