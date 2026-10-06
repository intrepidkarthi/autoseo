"""The outreach skip filter, and the bug that made it necessary.

Gemini reports each source by its registrable domain, so an App Store listing is stored as
`apple.com` and slipped past a skip-list that only knew `apps.apple.com`. Diarium's App Store page
was the #1 of 219 outreach targets on 2026-10-06: a competitor's own product listing, which no
email will ever change.
"""
from __future__ import annotations

import datetime as dt

import pytest

from autoseo.decide import outreach

# --- the skip filter ----------------------------------------------------------------------------

@pytest.mark.parametrize("domain, url, skipped", [
    ("apple.com", "https://apps.apple.com/us/app/diarium-journal-private-diary/id1436044299", True),
    ("apple.com", "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", True),
    ("apple.com", "https://itunes.apple.com/app/id1", True),
    ("apple.com", "https://www.apple.com/newsroom/2026/journal", False),   # resolved, not a store
    ("google.com", "https://play.google.com/store/apps/details?id=x", True),
    ("youtube.com", "https://m.youtube.com/watch?v=1", True),
    ("getdailyvox.com", "https://getdailyvox.com/blog/x", True),
    ("lound.ai", "https://lound.ai/blog/best-voice-journal-app-for-iphone-2026/", False),
    ("medium.com", "https://someone.medium.com/journal-apps", False),
    ("dropbox.com", "https://dropbox.com/x", False),                       # "x.com" is not a suffix
])
def test_is_skipped(domain, url, skipped):
    assert outreach.is_skipped(domain, url) is skipped


def test_app_store_listings_no_longer_top_the_outreach_list(db, monkeypatch):
    """The bug, end to end: 30 citations of a competitor's App Store page used to rank it #1."""
    from autoseo.core.db import session

    monkeypatch.setattr(outreach, "resolve", lambda url: url)
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with session() as conn:
        for i in range(30):
            conn.execute("INSERT INTO aeo_citation (ts, question_id, url, domain, title) VALUES "
                         "(?, ?, ?, 'apple.com', 'apple.com')",
                         (now, f"q{i % 5}", "https://apps.apple.com/us/app/diarium/id1436044299"))
        for i in range(3):
            conn.execute("INSERT INTO aeo_citation (ts, question_id, url, domain, title) VALUES "
                         "(?, ?, 'https://lound.ai/blog/best-voice-journal-app/', 'lound.ai', "
                         "'lound.ai')", (now, f"q{i}"))
        conn.commit()

    targets = outreach.build(days=30)
    assert [t.domain for t in targets] == ["lound.ai"]
    assert targets[0].rank == 1


def test_hyphenated_slugs_count_as_listicles():
    assert outreach._is_listicle("medium.com", "https://x.medium.com/3-apps-for-audio-journaling")
