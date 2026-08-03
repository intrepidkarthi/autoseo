# autoseo

SEO/AEO measurement and gated content automation for [DailyVox](https://getdailyvox.com).

Runs entirely on GitHub Actions. No server, no database to host, ~$0/month.

- **[DESIGN.md](DESIGN.md)** — architecture, module map, decision engine, quality gate
- **[SETUP.md](SETUP.md)** — every credential, with click-by-click steps

## Status

The blog pipeline runs end to end: measured demand in, pull request out, four
human gates in between.

| | | |
|---|---|---|
| measurement — GSC, URL Inspection, Bing | ✅ | validated to -1.2% against a UI export |
| AEO panel — buyer questions vs Gemini grounding | ✅ | free tier; records who gets cited instead |
| outreach targets — pages worth being listed on | ✅ | ranked from real citations |
| quality gate — slop, length, duplication | ✅ | 1,722-page shingle index |
| Telegram approval, instant via Cloudflare worker | ✅ | |
| blog publishing — PR to the site repo | ✅ | |
| finish de-listing the orphaned pages | ⬜ | no code needed; highest ROI available |
| social publishing (YouTube, Instagram) | ⬜ | |
| decision engine (bandit) | ⬜ | needs ~6 weeks of performance data |

X, Reddit and Quora are deliberately **manual**. They reward interaction with
other people's posts, not broadcast, and automating broadcast into them is how
accounts get filtered.

## Usage

**It runs offline with no credentials.** `state/*.csv` is committed, so `restore`
rebuilds the database from real production data and every read-only command works
with no network and no secrets. Credentials are only needed to collect *new* data.

```bash
pip install -e .
autoseo restore               # rebuild the db from committed state — always first
autoseo report                # per-cluster indexation ratio
autoseo brief                 # ranked actions with evidence
autoseo outreach              # pages worth getting listed on
```

Writing and publishing:

```bash
autoseo draft --top 1 --queue  # write against measured demand, send for approval
autoseo check FILE             # run the quality gate over any draft
autoseo publish --dry-run      # show the PR that would be opened
```

Everything is verified by `bash .claude/skills/run-autoseo/smoke.sh` — 13 checks
covering the CLI, the failure paths, direct invocation and the CSV round-trip.

## How the blog pipeline works

```
GSC demand  →  brief ranks by estimated click gain
            →  brand / competitor-internal / irrelevant queries excluded
            →  draft (Gemini free tier, 2 attempts)
            →  quality gate: slop, length, truncation, duplication vs 1,722 pages
            →  Telegram: full markdown as a file, inline, then a decision card
            →  your tap
            →  publish workflow (GitHub environment with a required reviewer)
            →  PR on the site repo
            →  your merge  →  Vercel deploys
```

Four independent gates. Cost: **$0** — Gemini's free tier covers drafting and the
AEO panel; everything else is free APIs.

## Design notes

**Statistics decide allocation, the LLM decides craft.** Scheduling is a multi-armed bandit — free,
deterministic, auditable. The model writes copy and judges freshness, nothing else.

**A module that reads the open web never holds publishing credentials.** Enforced by binding each
workflow to a different GitHub Environment, not by convention. A prompt injection reaching the
research step has no path to a posting token.

**Nothing publishes without human approval**, per channel, until a channel earns autonomy. Every
queued item carries a `policy` field so per-channel autonomy is a config change rather than a
rewrite.

**Posts start from measured demand, never from a topic someone thought of.** The input to a draft is
a query with real impressions where a page already sits at a reachable position.

## License

MIT. Third-party code is vendored rather than depended on — see `VENDOR.md` when it appears.
