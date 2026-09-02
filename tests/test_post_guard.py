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


def test_blocks_a_second_page_for_an_incumbent_claimed_this_run():
    """'travel journal app', 'travel diary app' and 'trip journal app' are one article, not three."""
    ours = [page("/blog/best-journal-app-for-travelers", 61, 48.2)]
    assert "already drafting" in _would_cannibalise(ours, {f"{SITE}/blog/best-journal-app-for-travelers"})


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
