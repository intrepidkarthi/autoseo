"""Question queries of five words and up: answer them on the page, or write the page."""
from __future__ import annotations

import datetime as dt

import pytest

from autoseo.core.db import session
from autoseo.decide import questions


@pytest.mark.parametrize("q, yes", [
    ("which journal app works offline", True),                       # 5 words, interrogative
    ("journal app that works without wifi?", True),                  # question mark
    ("best voice journal app for android", True),
    ("i need a private diary app", True),
    ("voice journal app for android", False),                        # 5 words, not a question
    ("encrypted journal app that values privacy", True),             # 6 words: counts unphrased
    ("island journal app for kids", False),                          # "is" is not "island"
    ("best journal app iphone", False),                              # four words
    ("which therapy software helps with session notes", False),      # off-topic
    ("best digital journaling app for beginners in france", False),  # a market template
    ("beste digitale lösungen für tägliches voice-journaling", False),  # German
    ("best wellness journaling app site:www.reddit.com", False),
    ("<brand>dazie</brand> journal app is criticised for pricing", False),
    ("compare voice-first journaling platforms that offer automated reminders", True),  # a prompt
])
def test_is_question(q, yes):
    assert questions.is_question(q) is yes


def test_people_lose_a_question_on_clicks_and_prompts_lose_it_by_being_shown():
    assert questions.losing("which journal app works offline", 100, 0, 5) is True
    assert questions.losing("which journal app works offline", 100, 6, 5) is False   # 6 of ~6
    assert questions.losing("compare voice-first journaling platforms that offer automated reminders",
                            100, 50, 5) is True


def _seed(table, rows):
    today = dt.date.today().isoformat()
    with session() as conn:
        for row in rows:
            if table == "gsc_query_daily":
                q, imp, clk, pos = row
                conn.execute("INSERT INTO gsc_query_daily (date, query, clicks, impressions, position) "
                             "VALUES (?,?,?,?,?)", (today, q, clk, imp, pos))
            else:
                page, q, imp, clk, pos = row
                conn.execute("INSERT INTO gsc_page_query (date, page, query, clicks, impressions, "
                             "position) VALUES (?,?,?,?,?,?)",
                             (today, f"https://getdailyvox.com{page}", q, clk, imp, pos))


def test_a_page_clicked_as_often_as_its_position_earns_is_not_strengthened(db):
    _seed("gsc_page_query", [
        ("/blog/offline", "which journal app works offline", 30, 0, 6),
        ("/blog/doing-fine", "which journal app works offline", 30, 3, 6),
    ])
    assert [p.slug for p in questions.by_page()] == ["offline"]


def test_wordings_too_small_alone_add_up_to_a_gap(db):
    _seed("gsc_query_daily", [
        ("is there a journal app for grief", 6, 0, 55),
        ("what journal app helps with grief", 5, 0, 60),
        ("best grief journal app for adults", 5, 0, 48),
        ("which journal app works offline", 40, 0, 6),               # we rank: not a gap
    ])
    gaps = questions.gaps()
    assert len(gaps) == 1 and gaps[0].kind == "question-gap"
    assert gaps[0].query == "is there a journal app for grief" and gaps[0].impressions == 16
    assert "3 wordings" in gaps[0].evidence


def test_a_purchase_prompt_backs_a_question_but_is_not_chosen():
    qs = [("sign me up for a subscription to an ai voice journaling service for daily habits", 30, 7),
          ("which ai voice journaling apps help build a daily habit?", 10, 7)]
    assert questions.representative(qs)[0].startswith("which")
