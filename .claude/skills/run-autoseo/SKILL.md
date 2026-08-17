---
name: run-autoseo
description: Run, build, test, drive or smoke-test autoseo — the SEO/AEO measurement CLI for getdailyvox.com. Use when asked to run autoseo, execute a command, check the indexation report, generate the action brief, inspect outreach targets, verify a change, or reproduce the pipeline locally.
---

# Running autoseo

`autoseo` is a Python CLI plus one GitHub Actions workflow. There is no server, no GUI and no
long-running process — every command runs to completion and exits.

**The one thing to know: it runs fully offline with no credentials.** `state/*.csv` is committed, so
`autoseo restore` rebuilds the local SQLite database from real production data and every read-only
command works with no network and no secrets. Credentials are only needed to *collect new* data.

All paths below are relative to the repo root.

**Driver: `bash .claude/skills/run-autoseo/smoke.sh`** — launches the CLI, drives every command,
checks exit codes, calls the internal functions directly, and verifies the CSV round-trip.

## Prerequisites

Python 3.12+. Nothing else — no system packages were needed.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Verified on macOS 15 / arm64 with Python 3.12.2. The dependencies (`google-api-python-client`,
`google-auth`, `httpx`, `defusedxml`) are pure-Python wheels, so Linux should be identical — but
this session did not verify Linux.

## Run (agent path)

```bash
bash .claude/skills/run-autoseo/smoke.sh            # full: reinstall + 15 checks
bash .claude/skills/run-autoseo/smoke.sh --quick    # skip reinstall
```

Exits non-zero if anything fails. Last verified run: **15 passed, 0 failed**.

It covers the two layers commits here actually touch:

1. **CLI surface** — `report`, `brief`, `outreach`, `opportunity`, `status`, `delist`,
   `aeo --dry-run`, `snapshot`, plus the failure paths.
2. **Direct invocation** — `decide/*`, `core/*`, and the autonomous layer: the caps, the ledger,
   and the two functions that edit a live page. Most changes here are to pure functions over the
   database, so this is the faster loop for a targeted fix.

To drive one thing by hand:

```bash
rm -f state/autoseo.db && .venv/bin/autoseo restore   # ALWAYS first — see Gotchas
.venv/bin/autoseo report            # indexation by cluster
.venv/bin/autoseo brief --top 5     # ranked actions with evidence
.venv/bin/autoseo outreach --top 5  # pages worth getting listed on
.venv/bin/autoseo status            # caps, ledger, what happens on the next run
```

The loop itself, without touching anything:

```bash
.venv/bin/autoseo plan --dry-run    # decide and compose, print instead of queueing
.venv/bin/autoseo apply --dry-run   # print every commit that would be made
.venv/bin/autoseo run --dry-run     # both, with a fresh collection first
```

Direct invocation, for a change to a pure function:

```bash
.venv/bin/python -c "
from autoseo.decide import brief
for a in brief.build(days=90)[:3]:
    print(a.priority, a.query, round(a.position,1), a.evidence[:60])"
```

## Commands that need credentials

These reach the network and exit 2 with an actionable message if the credential is missing.
They are normally run by CI, not locally.

| Command | Needs |
|---|---|
| `autoseo gsc [--backfill]`, `autoseo inspect`, `autoseo report` (fresh data) | `GSC_SERVICE_ACCOUNT_JSON` |
| `autoseo bing` | `BING_WEBMASTER_API_KEY` |
| `autoseo aeo`, `autoseo plan` (composing) | `GEMINI_API_KEY` |
| `autoseo apply`, `autoseo relink`, `autoseo delist --apply` | `GH_DAILYVOX_TOKEN` |

Locally they read a gitignored `.env` (see `.env.example`); in CI they come from the `compose`
GitHub Environment. `autoseo aeo --dry-run` exercises the panel logic with no key and no cost.

## Run in CI

```bash
gh workflow run seo.yml --repo intrepidkarthi/autoseo -f dry_run=true       # safe: changes nothing
gh workflow run seo.yml --repo intrepidkarthi/autoseo -f inspect_limit=50
```

`seo.yml` runs daily at 00:30 UTC. Two jobs: `plan` (environment `compose` — measures, decides,
composes, writes ledger rows) and `apply` (environment `publishing` — commits to the site repo).
Both `autoseo restore` at the start and `autoseo snapshot` at the end, then commit `state/`.

**Neither job pauses for a human, by design.** If `apply` is sitting on *Review pending*, a required
reviewer has been re-added to the `publishing` environment and every night's work is stuck behind it.

## Gotchas

These are the ones that cost real time here.

- **`autoseo restore` before anything else.** `state/autoseo.db` is gitignored; state lives in
  `state/*.csv`. Skip the restore and every command returns *empty results and exit 0* — which
  reads as "no data" rather than "you forgot a step". This is the single most likely way to
  misread the tool.
- **Sync from origin before any manual publish, or the caps lie.** `policy.post_budget()` reads the
  local database, and the local database is whatever you last restored. On 2026-08-17 a manual
  publish checked the budget at 06:54 UTC against state restored before CI's 02:47 push, was told
  "budget 1", and shipped a second post against a 1/day cap. The `apply` job does
  `git reset --hard origin/main` then `autoseo restore` before touching anything for exactly this
  reason. Do the same by hand: `git fetch && git reset --hard origin/main && autoseo restore`.
  The cap check is only as current as the rows behind it.

- **Empty output is often correct.** The site genuinely has ~16 clicks/90d of opportunity. Before
  debugging an empty list, check the thresholds in `decide/brief.py` and whether the date window
  contains data. A 10-day window put the best page at position 33.6 when its 3-month position was
  11.6 — short windows do not just add noise, they invert conclusions.
- **Never add `device` to a GSC dimension set.** `date+page` returns complete data; adding
  `device` silently drops ~87% of impressions below Google's privacy threshold. This looked like a
  collector bug for hours. `autoseo diagnose` exists to re-measure this.
- **Query-dimension data is a subset by design.** `gsc_query_daily` tops out around 1,098
  impressions against a true 7,828 because GSC withholds anonymised queries. Anything page-level
  must read `gsc_page_daily`. They are not rollups of each other.
- **zsh does not word-split unquoted variables.** `for c in "brief --top 1"; do autoseo $c; done`
  passes one argument and exits 2. Cost a false "the CLI is broken" diagnosis. Use arrays or bash.
- **SQLite must stay out of WAL mode.** `core/db.py` sets `journal_mode=DELETE` deliberately —
  WAL leaves committed rows in a side file, and the snapshot would miss the run's own data.
- **Gemini model IDs rot.** `gemini-2.5-flash` is still returned by ListModels but 404s for new
  keys ("no longer available to new users"). `aeo/probe.py` uses the moving alias
  `gemini-flash-latest`. Use `autoseo aeo --list-models` before pinning anything.
- **Gemini error bodies contain the API key**, so GitHub masks the whole message to `***` and the
  diagnosis vanishes. `probe.py` extracts `error.message` and strips the key first.
- **Grounding citations are redirects.** Gemini returns
  `vertexaisearch.cloud.google.com/grounding-api-redirect/...` with the real domain in the chunk
  *title*. Parsing the URI yields Google's host for every citation; the skip-list then discards
  every outreach target and the feature returns nothing. `db.migrate()` repairs this.
- **Only 8 of the 142 blog pages have markdown.** The other 134 are committed HTML with no source
  anywhere in the site repo, and they earn every impression the blog gets. `publish/blog.py` picks
  the path per page; `publish/page.py` is the HTML one. An on-page fixer restricted to the markdown
  8 would look correct and never fire — those 8 have zero impressions in 90 days between them.
- **The duplication corpus is not committed.** It is 5.7 MB and would be rewritten most weeks, so
  `plan` rebuilds it from the live site each run. Consequence: locally it is empty until you run
  `autoseo index-corpus --from-live`, and with an empty corpus `plan` refuses to write new posts at
  all rather than write unchecked ones.
- **The caps count queued work, not just shipped work.** `policy.post_budget()` subtracts items
  still sitting in the ledger. Without that, plan could compose three posts and apply could ship all
  three inside one morning while every individual cap still read as satisfied.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every command returns empty, exit 0 | No local database | `.venv/bin/autoseo restore` |
| `GSC returned 403` | Service account has Full, not Owner | URL Inspection needs Owner. Search Console → Settings → Users and permissions |
| `Gemini 404: ... no longer available to new users` | Pinned model retired | `autoseo aeo --list-models`, use a listed one or the `-latest` alias |
| `apply` exits 2 immediately | `GH_DAILYVOX_TOKEN` missing or expired (90-day life) | Regenerate the fine-grained PAT, `publishing` environment |
| `plan` composes nothing, ships nothing | Cap reached, or every page inside its 30-day cooldown | `autoseo status` says which |
| Nothing publishes but no error | `AUTOSEO_PAUSE` set, or `state/PAUSE` exists | `autoseo status` prints PAUSED first |
| `autoseo outreach` returns nothing but citations exist | `aeo_citation.domain` unrepaired | Reopen the db — `db.migrate()` repairs it. `smoke.sh` asserts this |
| Exit 2 on a command that printed fine | zsh word-splitting in your test loop | Quote properly, or use bash |
| Binary merge conflict on `state/` | An old checkout still tracks `autoseo.db` | It is gitignored now; `git rm --cached state/autoseo.db` |

## Test suite

There isn't one. `smoke.sh` is the regression net, and it is deliberately adversarial: the
outreach check asserts *"citations exist ⇒ targets exist"* rather than checking for a bad value,
because the failure mode is an empty list. An earlier version of that assertion passed while the
feature was dead.
