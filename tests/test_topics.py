"""A page already about the topic owns it, wherever it ranks.

Pinned on the posts the merge arm had to undo, and on the ones that turned out to be right.
"""
from __future__ import annotations

import pytest

from autoseo.decide import topics

PAGES = {s: topics.topic(s) for s in [
    "best-journal-app-for-travelers", "best-journal-app-iphone", "best-voice-journal-app",
    "voice-to-text-journal", "best-journal-app-for-privacy", "best-free-journal-app",
    "what-is-a-digital-twin", "how-to-journal-for-sleep", "best-ai-journal-app",
    "llm-that-cannot-cite-what-it-did-not-read",
]}


@pytest.mark.parametrize("query, page", [
    ("trip journal app", "best-journal-app-for-travelers"),        # 3 Oct -> merged 20 Oct
    ("journal apps for iphone", "best-journal-app-iphone"),        # 5 Oct -> merged 15 Oct
    ("What is the best voice to text journaling app?", "voice-to-text-journal"),
    ("What is the best audio journal app that transcribes what I say?", "best-voice-journal-app"),
    ("secure journal app", "best-journal-app-for-privacy"),
    ("best ai journaling app", "best-ai-journal-app"),            # 'ai' is two letters and counts
])
def test_a_page_already_about_the_topic_owns_it(query, page):
    assert topics.owner(query, PAGES)[0] == page


def test_the_head_term_is_never_free():
    assert topics.owner("best journaling app", PAGES) is not None


@pytest.mark.parametrize("query", [
    "digital journal apps market",                                 # one stray word is not ownership
    "Does journaling before bed help you sleep, and which app makes it easy?",
    "What is the best app for keeping a health journal to share with my doctor?",
    "Which AI apps cannot read what you tell them?",              # 'cannot' and 'read' are not a topic
])
def test_a_new_topic_is_free(query):
    assert topics.owner(query, PAGES) is None


def test_a_page_does_not_own_its_own_query():
    assert topics.owner("best journal app for travelers", PAGES,
                        exclude="best-journal-app-for-travelers") is None


@pytest.mark.parametrize("query", ["ai powered journal app", "ai assisted journaling app",
                                   "ai for journal entries"])
def test_filler_around_ai_does_not_make_a_new_topic(query):
    assert topics.owner(query, PAGES)[0] == "best-ai-journal-app"
