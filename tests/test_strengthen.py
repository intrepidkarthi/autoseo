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
