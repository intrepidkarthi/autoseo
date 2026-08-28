"""Invariants of the nightly workflow that are easy to break and expensive to notice.

Each assertion here stands for a failure that already happened once.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "seo.yml"


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def apply_steps(wf):
    return wf["jobs"]["apply"]["steps"]


def test_the_schedule_has_a_catch_up(wf):
    """One firing is one chance. GitHub dropped 2026-08-27 entirely — no run, no failure, no
    trace — and an unattended loop that skips a day silently looks like one with nothing to do."""
    crons = [c["cron"] for c in (wf.get("on") or wf[True])["schedule"]]
    assert len(crons) == 2, "the catch-up firing is gone"
    assert not any(c.split()[1] == "0" for c in crons), \
        "back on 00:xx UTC, where daily jobs pile up and runs get dropped"


def test_the_catch_up_is_gated(wf):
    """Two firings must not mean two full passes on an ordinary day."""
    assert wf["jobs"]["plan"]["needs"] == "guard"
    assert wf["jobs"]["plan"]["if"] == "needs.guard.outputs.proceed == 'true'"
    assert wf["jobs"]["apply"]["needs"] == "plan"


def test_the_guard_fails_open(wf):
    """A dropped run costs one day. A guard that wrongly reports 'already done' costs every day
    until somebody notices, and the premise of this workflow is that nobody is watching."""
    run = wf["jobs"]["guard"]["steps"][0]["run"]
    assert run.lstrip().startswith("echo \"proceed=true\""), \
        "proceed=true is no longer the default written before any check runs"


def test_the_guard_only_counts_scheduled_runs(wf):
    """A hand-dispatched run says nothing about whether the morning firing happened. Counting one
    would suppress the very catch-up it was dispatched to test."""
    assert '.event == \\"schedule\\"' in wf["jobs"]["guard"]["steps"][0]["run"]


def test_state_is_snapshotted_even_when_an_item_fails(apply_steps):
    """`apply` exits non-zero when an item fails, and `set -e` used to end the job there — before
    `snapshot` ran. On 2026-08-26 the prune that had already shipped reverted to `planned`."""
    step = next(s for s in apply_steps if s.get("id") == "apply")
    assert "autoseo apply $DRY || rc=$?" in step["run"], "the exit code is being allowed to abort"
    assert step["run"].index("autoseo snapshot") > step["run"].index("autoseo apply"), \
        "snapshot must run after apply, unconditionally"


def test_the_run_still_goes_red_but_last(apply_steps):
    """A failed item has to be visible. It just must not cost the ledger."""
    assert apply_steps[-1]["if"] == "steps.apply.outputs.rc != '0'"
    assert "exit 1" in apply_steps[-1]["run"]
    names = [s.get("name") for s in apply_steps]
    assert names.index("Commit state") < len(names) - 1, \
        "the failure step must come after the ledger is pushed"
