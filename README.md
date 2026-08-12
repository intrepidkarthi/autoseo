# autoseo

SEO for [DailyVox](https://getdailyvox.com), run without a person in the loop.

Every morning it measures the site, decides what is worth doing, writes the fix, checks its own work,
and commits to the site repo. No approval step, no notification, nothing to tap.

Runs entirely on GitHub Actions. No server, no database to host, ~$0/month.

- **[DESIGN.md](DESIGN.md)** — architecture, the loop, what stops it
- **[SETUP.md](SETUP.md)** — every credential, with click-by-click steps

## What it does each morning

```
measure     GSC · URL Inspection · Bing · sitemap inventory
decide      rank every (page, query) by estimated click gain
fix         ranked but not clicked      → rewrite the title and meta description
            page two, no FAQ            → append 3 answers, with FAQPage markup
            demand with no page close   → write a new post, 700-1000 words
            live page linked from       → link it from /blog
              nowhere
            1,507 dead templated pages  → noindex headers
            blog clusters earning zero   → noindex + drop from the sitemap
            404s and pagination in the   → drop from the sitemap
              sitemap
check       the full write-like-me rule set, plus duplication against the live corpus
ship        one atomic commit per fix on intrepidkarthi/dailyvox@main → Vercel deploys
submit      IndexNow → Bing, Yandex, Seznam, Naver. Google reads the regenerated sitemap
```

## Status

| | | |
|---|---|---|
| measurement — GSC, URL Inspection, Bing | ✅ | validated to -1.2% against a UI export |
| AEO panel — buyer questions vs Gemini grounding | ✅ | free tier; records who gets cited instead |
| outreach targets — pages worth being listed on | ✅ | ranked from real citations |
| quality gate — write-like-me, in code | ✅ | vendored scanner; every article, no exceptions |
| answer-engine gaps drive content | ✅ | 0 of 8 buyer questions mention us today |
| IndexNow submission | ✅ | Bing/Yandex/Seznam/Naver; Google has no equivalent API |
| autonomous loop — plan, apply, caps, ledger | ✅ | no approval anywhere in it |
| on-page fixer — titles, meta descriptions, FAQ | ✅ | works on both page kinds |
| publishing — direct commits, no PR | ✅ | daily |
| pruning — clusters that earn nothing | ✅ | the loop subtracts as well as adds |
| indexation tracked as a trend | ✅ | 69% today; the ceiling on everything |
| de-listing the 1,507 orphaned pages | ✅ | applied automatically |
| decision engine (bandit) | ⬜ | needs ~6 weeks of ledger rows paired with positions |
| video and social | ⏸️ | parked on purpose — the code stays, nothing schedules it |

X, Reddit and Quora remain deliberately **manual**. They reward interaction with other people's posts,
not broadcast, and automating broadcast into them is how accounts get filtered.

## The thing to watch

**94 of 140 blog pages are indexed.** A page that is not indexed cannot rank however good its title
is, and the 30 URLs Google has "discovered — currently not indexed" are it rationing crawl budget for
this domain. Publishing daily into that is a bet, so the loop now subtracts in the same run that it
adds — dead clusters get noindexed and dropped from the sitemap — and `autoseo status` leads with the
indexed ratio as a time series. If that number falls over the next few weeks, `MAX_POSTS_PER_DAY` in
`act/policy.py` is the dial.

## The limits, which are the interesting part

Removing the approval step means removing a rate limiter, so the rate limits are now explicit and in code:

| | |
|---|---|
| **≤1 post/day, ≤7/week** | counting queued *and* shipped, so plan can't stack a week into one morning |
| **≤2 on-page fixes/run, ≤5/week** | changing twenty titles in a week makes it impossible to attribute any movement to any of them |
| **30-day page cooldown** | search takes weeks to react to a title change; rewriting it daily measures nothing |
| **empty duplication corpus → no posts** | if it can't check for self-duplication it doesn't write |
| **14-day staleness drop** | a draft composed against numbers that have moved is dropped, not shipped |
| **path allowlist** | a commit touching anything outside four path prefixes raises before it is sent |
| **kill switch** | `AUTOSEO_PAUSE=1`, or commit a `state/PAUSE` file. Both halves stop immediately |

## Which engines this actually moves

| Engine | Measured by | Decided from | Submitted to |
|---|---|---|---|
| **Google** | Search Console + URL Inspection | every ranked (page, query) | sitemap + internal links — no API exists for ordinary pages |
| **Bing / Copilot** | Bing Webmaster (site totals) | — | IndexNow, on every change |
| **Yandex, Seznam, Naver** | — | — | IndexNow, same call |
| **ChatGPT, Gemini, Perplexity** | the AEO panel: 50 buyer questions, who gets named | uncited questions become posts | Bing's index is the upstream for Copilot |

Content alternates: two posts from Search Console demand, then one targeting a question where an
answer engine names Day One, Rosebud and Apple Journal and never names DailyVox. Left to rank by
evidence alone, Google would win every slot forever — it has impression counts and the answer-engine
panel has none.

## Usage

**It runs offline with no credentials.** `state/*.csv` is committed, so `restore` rebuilds the database
from real production data and every read-only command works with no network and no secrets.
Credentials are only needed to collect *new* data or to publish.

```bash
pip install -e .
autoseo restore               # rebuild the db from committed state — always first
autoseo status                # caps, ledger, what happens on the next run
autoseo report                # per-cluster indexation ratio
autoseo brief                 # ranked actions with evidence
autoseo outreach              # pages worth getting listed on
```

Driving the loop by hand:

```bash
autoseo run --dry-run         # the whole cycle, printing instead of committing
autoseo plan --dry-run        # decide and compose only
autoseo apply --dry-run       # show every commit that would be made
autoseo check FILE            # run the quality gate over any draft
```

Everything is verified by `bash .claude/skills/run-autoseo/smoke.sh` — 17 checks covering the CLI,
the failure paths, the caps, the quality gate, the page edits, the prune guards and the
CSV round-trip.

## How the two halves stay separated

```
plan   [environment: compose]     reads the open web, runs the model, holds NO site credential
  └─ writes rows to the ledger, each with its evidence and the gate's verdict
apply  [environment: publishing]  holds the site credential, runs NO model, reads no open web
  └─ executes those rows and commits
```

A prompt injection reaching the composing half has no path to a publishing token. This was the one
structural rule worth keeping when the human gate went away — and it matters more now, not less,
because nothing reads the output before it ships.

## Design notes

**Statistics decide allocation, the LLM decides craft.** What to work on is arithmetic over Search Console
data — free, deterministic, auditable. The model writes copy and nothing else.

**The record is the oversight.** Every action carries a rationale and lands in the ledger, snapshotted to
committed CSV. An action that cannot explain why it happened is a bug, and with nobody watching in real
time, that record is the only thing standing in for a person.

**Every article goes through the same gate, and the gate is code.** `write-like-me` is a skill — instructions
for an agent at a terminal. This pipeline publishes at 06:00 with nobody there, so the rule set runs as
`quality/`: fingerprints, the three vocabulary tiers, structure, rhythm, and a vendored copy of the skill's
own `scan_marks.py` for the marks that have no glyph. That last one is the reason it is vendored rather than
described — a model asked whether its own output contains zero-width characters will say it looks clean,
because it does.

**Two kinds of blog page.** 8 have markdown in `content/articles/` and are rendered; 134 are committed
HTML with no source anywhere in the site repo — and those 134 earn every impression the blog gets. The
fixer handles both, editing markdown where markdown is the source and HTML where it isn't.
