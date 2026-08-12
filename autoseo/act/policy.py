"""The limits that replaced the human gate.

A person tapping approve was, in practice, a rate limiter. Removing them without putting numbers in
their place is how an automated writer turns a site with eight real articles into a content farm —
which is the exact failure this site is still recovering from: 1,507 templated pages that earned 250
impressions and one click between them, and had to be pulled from the sitemap.

So the caps are conservative and they are enforced in code, not in a comment. Publishing is capped
per day *and* per week, because a weekly cap alone permits three posts in one morning.

`AUTOSEO_PAUSE=1`, or a file at `state/PAUSE`, stops everything. It is the one control worth keeping
after removing the rest: unattended systems need a switch that works without a deploy.
"""

from __future__ import annotations

import os

from autoseo.act import ledger
from autoseo.core.config import settings

MAX_POSTS_PER_DAY = 1
MAX_POSTS_PER_WEEK = 3

# On-page fixes, per run and per week. Small for two reasons. Each one is a commit against a page
# that already earns impressions, so a bad batch is harder to unpick than a bad new page. And
# changing twenty titles in a week destroys the ability to attribute any movement to any of them —
# the whole point of these edits is to learn which ones worked.
MAX_ONPAGE_FIXES_PER_RUN = 2
MAX_ONPAGE_FIXES_PER_WEEK = 5

# Do not touch the same page again inside this window. Search takes weeks to react to a title
# change; rewriting it daily measures nothing and looks like churn to a crawler.
PAGE_COOLDOWN_DAYS = 30


def paused() -> str:
    """Reason the loop is paused, or "" if it should run."""
    if os.environ.get("AUTOSEO_PAUSE", "").strip() not in ("", "0", "false"):
        return "AUTOSEO_PAUSE is set"
    pause_file = settings.state_dir / "PAUSE"
    if pause_file.exists():
        note = pause_file.read_text(encoding="utf-8").strip()
        return f"state/PAUSE exists{': ' + note if note else ''}"
    return ""


def post_budget() -> tuple[int, str]:
    """How many new posts may be planned right now, and the reason when that is zero.

    Counts what already shipped *and* what is planned but not yet applied. Without the second term,
    a plan run that queues three posts and an apply run that ships all three would satisfy every
    cap individually while publishing a week's worth in an hour.
    """
    today = ledger.shipped_since(ledger.Kind.POST, days=1)
    week = ledger.shipped_since(ledger.Kind.POST, days=7)
    queued = len(ledger.planned(ledger.Kind.POST))

    day_left = MAX_POSTS_PER_DAY - today - queued
    week_left = MAX_POSTS_PER_WEEK - week - queued
    budget = max(0, min(day_left, week_left))

    if budget:
        return budget, ""
    if queued:
        return 0, f"{queued} post(s) already composed and waiting to ship"
    if day_left <= 0:
        return 0, f"daily cap reached ({today}/{MAX_POSTS_PER_DAY} today)"
    return 0, f"weekly cap reached ({week}/{MAX_POSTS_PER_WEEK} in 7 days)"


def onpage_budget() -> int:
    """How many on-page fixes may be composed this run."""
    queued = len(ledger.planned(ledger.Kind.META)) + len(ledger.planned(ledger.Kind.FAQ))
    shipped_week = (ledger.shipped_since(ledger.Kind.META, days=7)
                    + ledger.shipped_since(ledger.Kind.FAQ, days=7))
    return max(0, min(MAX_ONPAGE_FIXES_PER_RUN - queued,
                      MAX_ONPAGE_FIXES_PER_WEEK - shipped_week - queued))


def cooling_down() -> set[str]:
    """Slugs edited recently enough that another edit would be noise."""
    return ledger.slugs_touched(
        (ledger.Kind.META, ledger.Kind.FAQ, ledger.Kind.POST), days=PAGE_COOLDOWN_DAYS
    )


def describe() -> str:
    budget, why = post_budget()
    lines = [
        f"  posts        {ledger.shipped_since(ledger.Kind.POST, 1)}/{MAX_POSTS_PER_DAY} today, "
        f"{ledger.shipped_since(ledger.Kind.POST, 7)}/{MAX_POSTS_PER_WEEK} this week "
        f"-> budget {budget}{'  (' + why + ')' if why else ''}",
        f"  on-page      {onpage_budget()} fix(es) available now "
        f"(cap {MAX_ONPAGE_FIXES_PER_RUN}/run, {MAX_ONPAGE_FIXES_PER_WEEK}/week; "
        f"{ledger.shipped_since(ledger.Kind.META, 7) + ledger.shipped_since(ledger.Kind.FAQ, 7)} "
        f"shipped in 7 days)",
        f"  cooldown     {len(cooling_down())} page(s) inside the {PAGE_COOLDOWN_DAYS}-day window",
    ]
    if reason := paused():
        lines.insert(0, f"  PAUSED       {reason}")
    return "\n".join(lines)
