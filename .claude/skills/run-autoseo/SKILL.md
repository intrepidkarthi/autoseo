---
name: run-autoseo
description: Run, build, test, drive or smoke-test autoseo — the SEO/AEO measurement CLI for getdailyvox.com. Use when asked to run autoseo, execute a command, check the indexation report, generate the action brief, inspect outreach targets, verify a change, or reproduce the pipeline locally.
---

# Running autoseo

`autoseo` is a Python CLI plus two GitHub Actions workflows. There is no server, no GUI and no
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
bash .claude/skills/run-autoseo/smoke.sh            # full: reinstall + 13 checks
bash .claude/skills/run-autoseo/smoke.sh --quick    # skip reinstall
```

Exits non-zero if anything fails. Last verified run: **13 passed, 0 failed**.

It covers the two layers commits here actually touch:

1. **CLI surface** — `report`, `brief`, `outreach`, `opportunity`, `gate --status`,
   `aeo --dry-run`, `snapshot`, plus the failure paths.
2. **Direct invocation** — `decide/*` and `core/*` called as functions. Most changes here are to
   pure functions over the database, so this is the faster loop for a targeted fix.

To drive one thing by hand:

```bash
rm -f state/autoseo.db && .venv/bin/autoseo restore   # ALWAYS first — see Gotchas
.venv/bin/autoseo report            # indexation by cluster
.venv/bin/autoseo brief --top 5     # ranked actions with evidence
.venv/bin/autoseo outreach --top 5  # pages worth getting listed on
.venv/bin/autoseo gate --status     # queue + telegram state
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
| `autoseo aeo` | `GEMINI_API_KEY` |
| `autoseo gate` (sending) | `TELEGRAM_BOT_TOKEN` |

Locally they read a gitignored `.env` (see `.env.example`); in CI they come from the `compose`
GitHub Environment. `autoseo aeo --dry-run` exercises the panel logic with no key and no cost.

## Run in CI

```bash
gh workflow run collect.yml --repo intrepidkarthi/autoseo -f inspect_limit=50 -f aeo_tier=skip
gh workflow run gate.yml    --repo intrepidkarthi/autoseo -f test_card=true
```

`collect.yml` runs daily at 00:30 UTC; `gate.yml` every 20 minutes. Both `autoseo restore` at the
start and `autoseo snapshot` at the end, then commit `state/`.

## Gotchas

These are the ones that cost real time here.

- **`autoseo restore` before anything else.** `state/autoseo.db` is gitignored; state lives in
  `state/*.csv`. Skip the restore and every command returns *empty results and exit 0* — which
  reads as "no data" rather than "you forgot a step". This is the single most likely way to
  misread the tool.
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
- **Telegram bots cannot message first.** Until you send `/start` to the bot, `gate` logs
  `No chat id` and delivers nothing — the run still exits 0.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every command returns empty, exit 0 | No local database | `.venv/bin/autoseo restore` |
| `GSC returned 403` | Service account has Full, not Owner | URL Inspection needs Owner. Search Console → Settings → Users and permissions |
| `Gemini 404: ... no longer available to new users` | Pinned model retired | `autoseo aeo --list-models`, use a listed one or the `-latest` alias |
| `No chat id` in `gate` | Bot never messaged | Send `/start` to the bot in Telegram |
| `autoseo outreach` returns nothing but citations exist | `aeo_citation.domain` unrepaired | Reopen the db — `db.migrate()` repairs it. `smoke.sh` asserts this |
| Exit 2 on a command that printed fine | zsh word-splitting in your test loop | Quote properly, or use bash |
| Binary merge conflict on `state/` | An old checkout still tracks `autoseo.db` | It is gitignored now; `git rm --cached state/autoseo.db` |

## Test suite

There isn't one. `smoke.sh` is the regression net, and it is deliberately adversarial: the
outreach check asserts *"citations exist ⇒ targets exist"* rather than checking for a bad value,
because the failure mode is an empty list. An earlier version of that assertion passed while the
feature was dead.
