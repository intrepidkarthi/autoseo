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


# --- backfill.yml: the whole-site edits, run by hand -------------------------------------------

BACKFILL = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "backfill.yml"


@pytest.fixture(scope="module")
def bf():
    return yaml.safe_load(BACKFILL.read_text())


def test_backfill_defaults_to_a_dry_run(bf):
    """An accidental click on the only credential that can change the live site should print a
    diff, not commit one."""
    inputs = (bf.get("on") or bf[True])["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True


def test_backfill_cannot_be_triggered_automatically(bf):
    """Dispatch only. These read all 156 blog pages to decide what to change, which is not a cost
    to pay nightly for something that needs doing once."""
    assert set(bf.get("on") or bf[True]) == {"workflow_dispatch"}


def test_backfill_holds_the_publishing_credential(bf):
    assert bf["jobs"]["run"]["environment"] == "publishing"


def test_backfill_serialises_against_the_nightly_loop(bf):
    """`site.commit` reads a head SHA before building its tree, so two writers racing on the site
    repo give a non-fast-forward to whichever loses."""
    assert bf["concurrency"]["group"] == "autoseo-state"


def test_every_backfill_target_is_a_real_command_that_takes_apply(bf, capsys):
    """The dropdown is a list of strings. Without this, a typo in it fails at the moment somebody
    clicks Run — which is the moment they least want to find out."""
    from autoseo.cli import main

    targets = (bf.get("on") or bf[True])["workflow_dispatch"]["inputs"]["target"]["options"]
    assert targets, "the target dropdown is empty"
    for target in targets:
        with pytest.raises(SystemExit) as exc:
            main([target, "--help"])
        assert exc.value.code == 0, f"`autoseo {target}` is not a command"
        help_text = capsys.readouterr().out
        assert "--apply" in help_text, f"`autoseo {target}` has no --apply"
        assert "--dry-run" in help_text, f"`autoseo {target}` has no --dry-run"
