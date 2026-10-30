"""Lost questions get a cause and one action; sources get Granola's classes."""
from __future__ import annotations

import pytest

from autoseo.decide import diagnose, topics


@pytest.mark.parametrize("domain, cls", [
    ("getdailyvox.com", "OWN"), ("github.com", "OWN"), ("youtube.com", "OWN"),
    ("apple.com", "CLAIM"), ("google.com", "CLAIM"), ("alternativeto.net", "CLAIM"),
    ("reddit.com", "EARN"),                     # an account is not ownership of the thread
    ("journalinghabit.com", "EARN"), ("rosebud.app", "COMPETITOR"), ("dayoneapp.com", "COMPETITOR"),
])
def test_classes(domain, cls):
    assert diagnose.classify(domain) == cls


def test_the_closest_page_is_found_even_where_the_duplicate_guard_would_say_none():
    owners = {s: topics.topic(s) for s in ["best-voice-journal-app", "best-offline-journal-app"]}
    assert diagnose._closest("What is the best voice journaling app for iPhone?", owners) == \
        "best-voice-journal-app"
    assert diagnose._closest("Which journal app works offline with no account?", owners) == \
        "best-offline-journal-app"
    assert diagnose._closest("Is there a private journal app for processing grief?", owners) == ""


def _seed(conn, qid, question, mentioned, cites=()):
    for run in range(3):
        conn.execute("INSERT INTO aeo_probe(ts, question_id, question, engine, run, mentioned, cited, "
                     "answer, competitors) VALUES (datetime('now'), ?, ?, 'g', ?, ?, 0, '', '[]')",
                     (qid, question, run, int(run < mentioned)))
    for d in cites:
        conn.execute("INSERT INTO aeo_citation(ts, question_id, url, domain, title) "
                     "VALUES (datetime('now'), ?, 'u', ?, ?)", (qid, d, d))


def test_causes_in_order(db, monkeypatch):
    from autoseo.core.db import session
    url = "https://getdailyvox.com/blog/best-voice-journal-app"
    monkeypatch.setattr(topics, "live_pages", lambda: {
        "best-voice-journal-app": topics.topic("best-voice-journal-app"),
        "best-offline-journal-app": topics.topic("best-offline-journal-app")})
    with session() as conn:
        conn.execute("INSERT INTO url_index_status(url, checked_at, coverage_state, indexed) "
                     "VALUES (?, '2026-10-29', 'Submitted and indexed', 1)", (url,))
        _seed(conn, "won", "What is the best voice journal app?", 3)
        _seed(conn, "missing", "Is there a journal app for processing grief?", 0)
        _seed(conn, "third", "What is the best voice diary app?", 0, ["reddit.com", "apple.com"])
        _seed(conn, "weak", "What is the best voice journaling app?", 0, ["rosebud.app"])
        _seed(conn, "indexing", "Is there an offline journal app?", 0)
    got = {x.question_id: x for x in diagnose.run()}
    assert "won" not in got
    assert got["missing"].cause == "missing page"
    assert got["indexing"].cause == "indexing"      # best-offline-journal-app was never inspected
    assert got["third"].cause == "third-party" and "u/getDailyVox" in got["third"].action
    assert got["weak"].cause == "weak page" and "/blog/best-voice-journal-app" in got["weak"].action
