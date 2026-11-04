"""Weak pages get a Quick answer at the top, not a rival page."""
from __future__ import annotations

from autoseo.act import ledger, onpage, plan, policy, strengthen
from autoseo.publish import page

ARTICLE = """<article>
  <h1>Best offline journal app</h1>
  <p>Intro.</p>
  <h2>Why offline</h2>
  <p>Body.</p>
  <h2>Frequently Asked Questions</h2>
</article>"""

BLOCK = "## Quick answer\n\nDailyVox works offline with no account. Day One suits people who want sync."


def test_a_quick_answer_goes_before_the_first_section():
    out = page.insert_lead(ARTICLE, BLOCK)
    assert out.index("<p>Intro.</p>") < out.index("<h2>Quick answer</h2>") < out.index("Why offline")


def test_planning_queues_one_lead_section_for_a_weak_page(db, monkeypatch):
    c = strengthen.Candidate("best-offline-journal-app",
                             "https://getdailyvox.com/blog/best-offline-journal-app",
                             "Is there a journaling app that works offline?", "evidence")
    monkeypatch.setattr(strengthen, "candidates", lambda: [c, c])
    monkeypatch.setattr(strengthen, "compose", lambda cand, text: BLOCK)
    fresh = onpage.LivePage("t", "d", "Intro text", False, False)
    monkeypatch.setattr(onpage, "live_page", lambda url: fresh)
    monkeypatch.setattr(policy, "cooling_down", lambda: set())
    result = plan.Planned()
    plan._plan_strengthen(result, dry_run=False)
    items = ledger.planned(ledger.Kind.SECTION)
    assert result.strengthened == 1 and len(items) == 1          # one a run
    assert items[0].meta["position"] == "lead" and items[0].meta["source"] == "diagnose"
    assert policy.strengthen_budget() == 0                         # the queued one counts


def test_a_page_that_already_has_one_is_left_alone(db, monkeypatch):
    c = strengthen.Candidate("x", "https://getdailyvox.com/blog/x", "q", "e")
    monkeypatch.setattr(strengthen, "candidates", lambda: [c])
    monkeypatch.setattr(strengthen, "compose", lambda cand, text: BLOCK)
    already = onpage.LivePage("t", "d", "Title Quick answer DailyVox works", False, False)
    monkeypatch.setattr(onpage, "live_page", lambda url: already)
    monkeypatch.setattr(policy, "cooling_down", lambda: set())
    result = plan.Planned()
    plan._plan_strengthen(result, dry_run=False)
    assert result.strengthened == 0


def _seed(rows):
    import datetime as dt

    from autoseo.core.db import session
    today = dt.date.today().isoformat()
    with session() as conn:
        for page, query, imp, pos in rows:
            conn.execute("INSERT INTO gsc_page_query (date, page, query, clicks, impressions, position) "
                         "VALUES (?,?,?,0,?,?)", (today, f"https://getdailyvox.com{page}", query, imp, pos))


LONG = "compare voice-first journaling platforms that offer automated reminders for personal growth."


def test_long_queries_ask_each_page_its_biggest_eight_word_query(db):
    _seed([
        ("/blog/best-voice-journal-app", LONG, 21, 7.3),
        ("/blog/best-voice-journal-app", "recommend the best voice-first apps for a journaling habit.", 5, 9),
        ("/blog/best-voice-journal-app", "voice journal app", 400, 3),               # a keyword
        ("/blog/page/3", LONG, 50, 7),                                                # a listing
        ("/blog/deep", "which journaling app is best for someone who hates typing?", 30, 24),
        ("/blog/thin", "which journaling app is best for someone who hates typing?", 4, 5),
        ("/blog/thin", "what journaling app is best for someone who hates typing?", 5, 5),
        ("/blog/de", "zeig mir seiten, wo ich direkt einen dienst für voice-journaling buchen kann.", 40, 6),
        ("/blog/best-voice-journal-app",
         "what is the most affordable digital journaling app with templates and reminders in spain?",
         90, 5),                                                                      # a market template
        ("/blog/notes", "which therapy practice systems make it easiest to sign notes between sessions?",
         40, 6),                                                                      # off-topic
        ("/blog/tpl", "<brand>dazie (dazie.io)</brand> is criticised for <objection>high pricing</objection>",
         40, 1),
    ])
    found = strengthen.long_queries()
    assert [c.slug for c in found] == ["best-voice-journal-app"]
    assert found[0].question == LONG and found[0].source == "gsc"
    assert "26 times" in found[0].evidence and "2 question queries" in found[0].evidence


def test_the_two_sources_alternate_and_a_page_is_asked_once(monkeypatch):
    def cand(slug, source):
        return strengthen.Candidate(slug, f"https://getdailyvox.com/blog/{slug}", "q", "e", source)
    monkeypatch.setattr(strengthen, "weak_pages", lambda: [cand("a", "diagnose"), cand("b", "diagnose")])
    monkeypatch.setattr(strengthen, "long_queries", lambda: [cand("a", "gsc"), cand("c", "gsc")])
    assert [(c.slug, c.source) for c in strengthen.candidates()] == [
        ("a", "diagnose"), ("b", "diagnose"), ("c", "gsc")]


def test_long_query_items_share_the_budget(db, monkeypatch):
    c = strengthen.Candidate("x", "https://getdailyvox.com/blog/x", LONG, "e", "gsc")
    monkeypatch.setattr(strengthen, "candidates", lambda: [c])
    monkeypatch.setattr(strengthen, "compose", lambda cand, text: BLOCK)
    monkeypatch.setattr(onpage, "live_page", lambda url: onpage.LivePage("t", "d", "Intro", False, False))
    monkeypatch.setattr(policy, "cooling_down", lambda: set())
    plan._plan_strengthen(plan.Planned(), dry_run=False)
    assert ledger.planned(ledger.Kind.SECTION)[0].meta["source"] == "gsc"
    assert policy.strengthen_budget() == 0


SHIPPED_2026_11_03 = (
    "Cloud-based AI companions log your voice on remote servers, which fails high-pressure work where "
    "confidentiality is non-negotiable. Apps like Rosebud suit people who want guided cloud feedback. "
    "I built DailyVox for total privacy. Nothing leaves the phone. The catch is clear.")


def test_the_block_that_never_answered_is_refused():
    problems = strengthen.shape_problems(SHIPPED_2026_11_03)
    assert any("names no app" in p for p in problems)
    assert any("teaser" in p for p in problems)


def test_an_answer_first_block_ending_on_its_trade_off_passes():
    good = ("DailyVox is a free voice journal that transcribes on the phone. Day One suits people "
            "who want sync. DailyVox has no desktop app, and entries do not sync between platforms.")
    assert strengthen.shape_problems(good) == []


def test_a_page_is_asked_the_question_its_traffic_shares_not_its_one_off():
    from autoseo.decide import questions
    qs = [("compare ai companionship services for voice journaling in high-pressure work", 24, 7),
          ("which voice journaling apps help build a daily reflection habit?", 11, 7),
          ("best voice journaling apps for building a daily reflection habit", 12, 8),
          ("recommend voice journaling apps for a consistent daily reflection habit", 8, 6)]
    assert "habit" in questions.representative(qs)[0]


def test_replace_section_swaps_only_that_section():
    doc = ("<article>\n  <p>Intro.</p>\n\n  <h2>Quick answer</h2>\n  <p>Old one.</p>\n\n"
           "  <h2>How These Apps Were Tested</h2>\n  <p>Body.</p>\n</article>")
    out = page.replace_section(doc, "## Quick answer\n\nNew one.")
    assert "Old one" not in out and out.count("<h2>Quick answer</h2>") == 1
    assert "  <p>New one.</p>" in out and out.index("New one") < out.index("How These Apps")
    assert "<p>Body.</p>" in out
