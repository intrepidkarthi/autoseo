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
expect 0 "autoseo status"            -- "$CLI" status
expect 0 "autoseo aeo --dry-run"     -- "$CLI" aeo --dry-run
expect 0 "autoseo delist"            -- "$CLI" delist
expect 0 "autoseo snapshot"          -- "$CLI" snapshot

step "3. failure paths must fail loudly, not silently"
# A missing credential has to exit non-zero with an actionable message. Silent success on
# no data is how this project previously shipped a dead feature.
( unset GSC_SERVICE_ACCOUNT_JSON; expect 2 "autoseo gsc without credentials" -- "$CLI" gsc )
( unset GH_DAILYVOX_TOKEN; expect 2 "autoseo apply without the site token" -- "$CLI" apply )
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

step "5. the autonomous loop — caps, ledger, and page edits"
# Nothing here touches the network. These are the parts that now run with nobody watching:
# the caps that replaced the approval, and the two functions that edit a live page.
"$PY" - <<'EOF' && ok "policy, ledger and page edits behave" || bad "autonomous loop layer failed"
import re
from autoseo.act import ledger, onpage, policy
from autoseo.publish import page

# --- caps. A cap that does not count what is already queued is not a cap: plan would compose three
# posts and apply would ship all three inside one morning.
budget, why = policy.post_budget()
assert budget <= policy.MAX_POSTS_PER_DAY, f"budget {budget} exceeds the daily cap"
before = budget
item_id = ledger.plan(ledger.Item(kind=ledger.Kind.POST, title="smoke", body="x",
                                  rationale="smoke test", meta={"slug": "smoke-test"}))
after, _ = policy.post_budget()
assert after == max(0, before - 1), f"queued post did not consume budget: {before} -> {after}"
assert "smoke-test" in policy.cooling_down(), "a planned page is not inside the cooldown window"
ledger.drop(item_id, "smoke test")
assert policy.post_budget()[0] == before, "dropping did not release the budget"
# Leave no trace: state/queue_item.csv is committed, and a smoke row in it would be indistinguishable
# from a real decision when someone reads the history six weeks from now.
from autoseo.core.db import session as _session
with _session() as _c:
    _c.execute("DELETE FROM queue_item WHERE id = ?", (item_id,))

# --- pause switch. The one control left; if it silently stopped working nothing would say so.
import os
os.environ["AUTOSEO_PAUSE"] = "1"
assert policy.paused(), "AUTOSEO_PAUSE=1 did not pause the loop"
del os.environ["AUTOSEO_PAUSE"]
assert not policy.paused(), "the pause did not clear"

# --- candidate selection. Three regressions live here, all of them shipped once.
from autoseo.decide.brand import classify
from autoseo.decide.brief import expected_ctr
for c in onpage.candidates(days=90):
    assert not c.slug.isdigit(), f"pagination selected as a candidate: {c.url}"
    assert "/blog/page/" not in c.url, f"pagination selected as a candidate: {c.url}"
    # A rewrite driven by a brand query optimises a page for people already looking for us, and an
    # empty query asks the model to target nothing while the keep-a-query-term check passes
    # vacuously. Both selected real pages before this filter.
    assert c.query, f"candidate with no query: {c.url}"
    assert classify(c.query) == "acquisition", \
        f"{classify(c.query)} query '{c.query}' selected {c.url}"
    # Position must be the query-level one, never the page average. /blog/best-voice-journal-app
    # averages 12.9 while sitting at 34-42 on every query it was written for — the average is
    # manufactured by anonymised long-tail terms, and selecting on it retitled that exact page.
    if c.kind == "meta":
        assert c.position <= onpage.META_MAX_POSITION, \
            f"meta candidate at position {c.position:.1f}, above the visibility threshold"
        assert c.ctr < expected_ctr(c.position) * onpage.CTR_SHORTFALL, \
            f"{c.url} is not actually under-clicked for its position"

# --- page edits, against markup shaped like the real pages
DOC = '''<html><head>
  <title>Old Title</title>
  <meta name="description" content="old description">
  <meta property="og:title" content="Old Title">
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"BlogPosting","headline":"Old Title",
   "description":"old description","dateModified":"2020-01-01"}
  </script>
</head><body><main><article class="blog-article"><div class="article-body">
  <p>Body copy that must survive untouched.</p>
  <div class="article-cta"><h3>Try it</h3></div>
</div></article></main></body></html>'''

new = page.retitle(DOC, 'Fixed "Title" & Co', "A better description of the page.")
assert "<title>Fixed &quot;Title&quot; &amp; Co</title>" in new, "title tag not rewritten or not escaped"
assert 'content="A better description of the page."' in new, "meta description not rewritten"
assert "Body copy that must survive untouched." in new, "retitle changed the body"
import json
ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', new, re.S).group(1))
assert ld["headline"] == 'Fixed "Title" & Co', "structured data disagrees with the title tag"
assert ld["dateModified"] != "2020-01-01", "dateModified not bumped"

block = "## Frequently asked questions\n\n### Does it work offline?\n\nYes, entirely on device.\n\n### Where is data stored?\n\nOn the phone."
pairs = page.parse_faq(block)
assert len(pairs) == 2, f"parsed {len(pairs)} Q/A pairs, expected 2"
faq = page.append_faq(DOC, pairs)
assert faq.index("Frequently asked questions") < faq.index('class="article-cta"'), \
    "FAQ inserted after the call to action"
assert '"@type": "FAQPage"' in faq, "FAQPage structured data not added"
assert "Body copy that must survive untouched." in faq, "append_faq changed the body"
try:
    page.append_faq(faq, pairs)
except RuntimeError:
    pass
else:
    raise AssertionError("appending a second FAQ block was allowed")
EOF

step "6. the quality gate — every article goes through this and nothing else"
# The gate is the only thing between the model and the live site. Two properties matter: it strips
# what has one correct fix, and it blocks what does not.
"$PY" - <<'EOF' && ok "gate strips marks, blocks fingerprints, passes clean prose" || bad "quality gate failed"
from autoseo.quality import gate, marks

# The scanner must be present. A provenance check that silently no-ops would record a clean
# verdict for a check that never ran — the exact failure the empty duplication corpus already had.
assert marks.SCANNER.exists(), f"vendored scanner missing at {marks.SCANNER}"

ZWSP, NBSP = "​", " "
dirty = f"""---
slug: probe
generator: ChatGPT
---

# A probe

Great question! DailyVox{ZWSP}transcribes on{NBSP}device. Studies show it matters.
See https://example.com/x?utm_source=chatgpt.com for more. Written by [Your Name].
"""
v = gate.evaluate(dirty, context="section", check_duplication=False)
assert not v.passed, "contaminated draft passed the gate"
blocked = " ".join(v.reasons).lower()
for expected in ("placeholder", "chatbot artifact", "vague attribution", "sycophancy"):
    assert expected in blocked, f"{expected!r} not blocked — reasons were: {v.reasons}"

# The utm parameter is stripped rather than blocked, and that split is the design: a tracking
# param has exactly one correct fix, an unfilled placeholder has none that a machine may apply.
assert "utm_source=chatgpt" not in v.text, "AI utm parameter survived into the returned text"
assert any("utm" in w.lower() or "fingerprint" in w.lower() for w in v.warnings), \
    f"utm strip not reported — warnings were: {v.warnings}"

# Sanitised text is what callers publish, so the marks must actually be gone from it.
assert ZWSP not in v.text, "zero-width space survived into the returned text"
assert NBSP not in v.text, "no-break space survived into the returned text"
assert "generator: ChatGPT" not in v.text, "provenance key survived into the returned text"
assert v.marks_stripped >= 2, f"expected marks stripped, got {v.marks_stripped}"

# Load-bearing joiners must survive: stripping these mangles emoji and Tamil.
keeps = marks.sanitise("family 👨‍👩‍👧 and தமிழ்")
assert keeps.kept >= 2, f"contextual joiners were stripped ({keeps.kept} kept)"
assert "👨‍👩‍👧" in keeps.text, "emoji ZWJ sequence was broken"

# And the house style must not trip it. Short sentences next to long ones are the voice profile,
# not a defect — an earlier draft of the fragmentation rule flagged exactly this.
house = ("Turn on airplane mode. Speak. The transcript appears. That is the whole test, and it is "
         "the only one I trust, because it is the only one you can run without taking my word for "
         "anything at all.")
report = __import__("autoseo.quality.slop", fromlist=["slop"]).analyse(house, context="section")
assert not report.p0, f"house style blocked: {[f.rule for f in report.p0]}"
assert not any(f.rule == "dramatic fragmentation" for f in report.flags), \
    "varied short sentences flagged as dramatic fragmentation"
EOF

step "7. subtraction — the only irreversible-feeling thing the loop does"
# Pruning switches pages off. Every guard here exists because the alternative is a threshold change
# quietly taking out the cluster that earns 624 impressions.
"$PY" - <<'EOF' && ok "prune guards hold, sitemap edits are surgical" || bad "subtraction layer failed"
from autoseo.decide import prune
from autoseo.publish import sitemap

# The protected prefixes must match what _prefix_of actually returns. The first version of the
# tuple carried trailing hyphens, so startswith() never matched and the guard on the blog's best
# cluster silently did nothing — it survived on the impressions threshold alone.
for p in prune.PROTECTED:
    assert not p.endswith("-"), f"PROTECTED entry {p!r} has a trailing hyphen and will never match"
sample = "https://getdailyvox.com/blog/dailyvox-vs-notion"
assert prune._prefix_of(sample).startswith(prune.PROTECTED), \
    f"protection does not fire for {sample}: prefix is {prune._prefix_of(sample)!r}"

# Nothing protected, nothing earning, may ever be proposed for pruning.
dead = prune.dead_clusters(90)
for c in dead:
    assert not c.prefix.startswith(prune.PROTECTED), f"protected cluster proposed: {c.prefix}"
    assert c.clicks <= prune.MAX_CLICKS, f"{c.prefix} has {c.clicks} clicks and was proposed"
    assert c.impressions / c.pages <= prune.MAX_IMPRESSIONS_PER_PAGE, \
        f"{c.prefix} is above the impressions floor and was proposed"
    assert c.pages >= prune.MIN_PAGES, f"{c.prefix} is too small to read as a cluster"

# Prefix bucketing: `for`/`vs` are joiners, so the cluster is three segments, not two.
assert prune._prefix_of("https://x/blog/journal-prompts-for-anger") == "journal-prompts-for"
assert prune._prefix_of("https://x/blog/voice-journal-for-runners") == "voice-journal-for"
assert prune._prefix_of("https://x/blog/how-to-export-day-one") == "how-to"

# Sitemap editing removes exactly the named entries and touches nothing else.
XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://getdailyvox.com/blog</loc><lastmod>2026-07-05</lastmod></url>
  <url><loc>https://getdailyvox.com/blog/page/2</loc><lastmod>2026-07-05</lastmod></url>
  <url><loc>https://getdailyvox.com/blog/keep-me</loc><lastmod>2026-07-05</lastmod></url>
</urlset>
"""
out, gone = sitemap.remove(XML, {"https://getdailyvox.com/blog/page/2"})
assert gone == ["https://getdailyvox.com/blog/page/2"], gone
assert "keep-me" in out and "/blog</loc>" in out, "removal took an unrelated entry"
assert "page/2" not in out, "target entry survived"
assert out.startswith('<?xml') and out.rstrip().endswith("</urlset>"), "document structure broken"
# Idempotent: removing something already gone is a no-op, not an error.
again, gone2 = sitemap.remove(out, {"https://getdailyvox.com/blog/page/2"})
assert again == out and not gone2

# A page being switched off must never be submitted for recrawl in the same run.
from autoseo.act import apply as applier, ledger
for kind in (ledger.Kind.PRUNE, ledger.Kind.SITEMAP):
    item = ledger.Item(kind=kind, title="x", body="", rationale="x", meta={"slug": "some-slug"})
    assert applier._urls_for(item) == set(), f"{kind} would be submitted to IndexNow"
EOF

step "8. snapshot round-trip must be lossless"
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
