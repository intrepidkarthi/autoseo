#!/usr/bin/env bash
# autoseo smoke driver — launches the real CLI and drives it, offline.
#
# The whole point: autoseo needs NO credentials to run. state/*.csv is committed, so
# `autoseo restore` rebuilds the database and every read-only command works against real
# production data with no network and no secrets. That makes this the fast dev loop.
#
# Covers the two layers PRs actually touch:
#   1. the CLI surface        — every command a user types, plus exit codes
#   2. direct invocation      — decide/* and core/* called as functions, which is what
#                               most commits here modify (probe, snapshot, db, brief)
#
# Usage:  bash .claude/skills/run-autoseo/smoke.sh [--quick]
#         --quick skips reinstall (assumes .venv is current)

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../../.." || exit 1
ROOT=$PWD
VENV=$ROOT/.venv
PY=$VENV/bin/python
CLI=$VENV/bin/autoseo
QUICK=${1:-}

pass=0; fail=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# expect <exit-code> <label> -- <command...>
expect() {
  local want=$1 label=$2; shift 3
  "$@" >/tmp/autoseo_smoke.out 2>&1
  local got=$?
  if [ "$got" -eq "$want" ]; then ok "$label (exit $got)"
  else bad "$label — wanted exit $want, got $got"; tail -3 /tmp/autoseo_smoke.out | sed 's/^/        /'; fi
}

step "0. environment"
[ -d "$VENV" ] || python3 -m venv "$VENV"
if [ "$QUICK" != "--quick" ]; then
  "$VENV/bin/pip" install -q -e . 2>&1 | tail -2
fi
"$PY" -c "import autoseo" 2>/dev/null && ok "package imports" || { bad "package does not import"; exit 1; }

step "1. restore — rebuild the db from committed CSV (no credentials, no network)"
# The sqlite file is gitignored on purpose. Skip this and every query returns empty,
# which reads as "no data" rather than "you forgot to restore".
rm -f state/autoseo.db
expect 0 "autoseo restore" -- "$CLI" restore
rows=$("$PY" - <<'EOF'
from autoseo.core.db import session
with session() as c:
    print(c.execute("SELECT COUNT(*) FROM gsc_page_daily").fetchone()[0])
EOF
)
[ "${rows:-0}" -gt 100 ] && ok "restored $rows page-day rows" || bad "restore produced only ${rows:-0} rows"

step "2. CLI surface — every read-only command"
expect 0 "autoseo report"            -- "$CLI" report
expect 0 "autoseo brief"             -- "$CLI" brief --top 3
expect 0 "autoseo outreach"          -- "$CLI" outreach --top 3
expect 0 "autoseo opportunity"       -- "$CLI" opportunity
expect 0 "autoseo gate --status"     -- "$CLI" gate --status
expect 0 "autoseo aeo --dry-run"     -- "$CLI" aeo --dry-run
expect 0 "autoseo snapshot"          -- "$CLI" snapshot

step "3. failure paths must fail loudly, not silently"
# A missing credential has to exit non-zero with an actionable message. Silent success on
# no data is how this project previously shipped a dead feature.
( unset GSC_SERVICE_ACCOUNT_JSON; expect 2 "autoseo gsc without credentials" -- "$CLI" gsc )
expect 2 "unknown subcommand"        -- "$CLI" definitely-not-a-command

step "4. direct invocation — the layer most commits touch"
"$PY" - <<'EOF' && ok "decide/* and core/* callable directly" || bad "direct invocation failed"
import sys
from autoseo.decide import brief, outreach, brand
from autoseo.core import snapshot

# brand classification is pure and is what silently broke the CTR analysis once
assert brand.classify("dailyvox") == "brand", "brand query not detected"
assert brand.classify('"id widgetkit" android') == "irrelevant", "iOS-identifier noise not filtered"
assert brand.classify("best voice journal app") == "acquisition", "real query misclassified"

actions = brief.build(days=90)
assert isinstance(actions, list), "brief.build did not return a list"
if actions:
    a = actions[0]
    assert a.evidence, "action has no evidence — a card that cannot explain itself is a bug"
    assert a.est_click_gain >= 0

targets = outreach.build(days=30, resolve_top=0)   # resolve_top=0 keeps this offline

# The invariant that matters: citations exist => targets exist. When domain extraction broke,
# every row became a vertexaisearch redirect, the skip-list discarded all of them, and outreach
# returned an EMPTY list — which reads as "nothing to pitch" rather than "the feature is dead".
# Asserting the absence of the bad domain was useless: an empty set contains nothing at all.
from autoseo.core.db import session as _s
with _s() as _c:
    n_cit = _c.execute("SELECT COUNT(*) FROM aeo_citation").fetchone()[0]
if n_cit > 50:
    assert targets, (
        f"{n_cit} citations stored but outreach returned 0 targets — "
        "domain extraction is broken (check aeo_citation.domain)"
    )

assert snapshot.TABLES, "snapshot has no tables registered"
print(f"    brief={len(actions)} actions  outreach={len(targets)} targets", file=sys.stderr)
EOF

step "5. snapshot round-trip must be lossless"
"$PY" - <<'EOF' && ok "CSV round-trip lossless" || bad "CSV round-trip lost rows"
import os
from autoseo.core import snapshot
from autoseo.core.db import session

MEASUREMENT = [t for t in snapshot.TABLES if t not in ("run_log",)]  # run_log logs its own dump
with session() as c:
    before = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MEASUREMENT}
snapshot.dump()
os.remove("state/autoseo.db")
snapshot.load()
with session() as c:
    after = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MEASUREMENT}
bad_tables = {t: (before[t], after[t]) for t in before if before[t] != after[t]}
assert not bad_tables, f"row counts changed across round-trip: {bad_tables}"
EOF

step "result"
printf '  %d passed, %d failed\n\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
