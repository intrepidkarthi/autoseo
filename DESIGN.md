# autoseo — Design

SEO for getdailyvox.com, run without a person in the loop. Measure, decide, fix, publish, on a daily
schedule. Runs entirely on GitHub Actions. No server, $0/month.

Video and social publishing are **parked** — the code is still here, nothing schedules it.

---

## 1. Principles

1. **Statistics decide allocation, the LLM decides craft.** What to work on is arithmetic over Search
   Console data — free, deterministic, auditable. The model writes copy and nothing else.
2. **Caps replace the gatekeeper.** There is no approval step. What stands in its place is a quality gate
   that blocks, publishing caps that are enforced in code, a cooldown per page, and a kill switch. A
   person tapping *approve* was, in practice, a rate limiter; the rate limits are now explicit.
3. **A module that reads the open web never holds publishing credentials.** The prompt-injection firewall,
   enforced by binding each job to a different GitHub Environment. This survived the removal of the human
   gate because it never depended on it.
4. **Vendor, don't depend.** Third-party MIT code is copied in with its header, pinned, and listed in
   `VENDOR.md`. No upstream churn, no submodules.
5. **Every automated decision explains itself.** The ledger row carries the evidence that produced it. With
   nobody watching in real time, the record *is* the oversight.
6. **Free collectors are never gated by budget.** The measurement series must never have a hole.

---

## 2. Module map

```
autoseo/
├── .github/workflows/
│   └── seo.yml                 # 06:00 IST daily. Two jobs, different secret sets:
│                               #   plan   [compose env]    — measure, decide, compose
│                               #   apply  [publishing env] — commit to the site repo
│
├── autoseo/
│   ├── core/
│   │   ├── config.py           # env → typed settings. Single source of truth.
│   │   ├── db.py               # SQLite connection + schema
│   │   ├── snapshot.py         # db ⇄ state/*.csv, the git-mergeable persistence
│   │   └── log.py              # structured logging
│   │
│   ├── collect/                # ← reads the world. NO write credentials, ever.
│   │   ├── gsc.py              # Search Analytics API
│   │   ├── inspect.py          # URL Inspection API (2,000/day) → indexed vs not
│   │   ├── bing.py             # Bing Webmaster Tools API
│   │   ├── inventory.py        # sitemap → URL inventory, bucketed by cluster
│   │   └── diagnose.py         # isolate where GSC impressions go missing
│   │
│   ├── decide/                 # ← pure functions. No network, no LLM, no credentials.
│   │   ├── brand.py            # brand / competitor-internal / irrelevant classification
│   │   ├── opportunity.py      # striking distance, CTR gaps, content gaps
│   │   ├── brief.py            # ranked actions with evidence
│   │   └── outreach.py         # pages worth being listed on, from real citations
│   │
│   ├── act/                    # ← the loop
│   │   ├── plan.py             # decide → compose → gate → ledger.  Publishes nothing.
│   │   ├── apply.py            # ledger → commits.  Composes nothing, reads no open web.
│   │   ├── onpage.py           # which live pages to fix, and the composed fix
│   │   ├── policy.py           # the caps, the cooldown, the kill switch
│   │   └── ledger.py           # what was decided, why, and what happened
│   │
│   ├── compose/
│   │   ├── llm.py              # provider-agnostic, tiered: free → cheap → premium
│   │   ├── blog.py             # → markdown for render_articles.py
│   │   └── video.py            # parked
│   │
│   ├── quality/                # ← the gate that blocks. Regex + arithmetic, so $0.
│   │   ├── marks.py            # invisible Unicode, paste fingerprints, frontmatter provenance
│   │   ├── slop.py             # AI-ism rules, length floor, truncation, stylometry, hard-nos
│   │   ├── plagiarism.py       # shingled 5-grams vs the live corpus (hashes only)
│   │   └── gate.py             # P0 blocks; P1 must reach 6
│   │
│   ├── publish/                # ← holds write credentials. NEVER reads the open web.
│   │   ├── site.py             # git data API: one atomic commit per action, path-allowlisted
│   │   ├── blog.py             # render → commit; retitle; append a section; relink orphans
│   │   ├── page.py             # surgical head edits for pages with no markdown source
│   │   ├── blog_index.py       # insert/update the entry on /blog
│   │   ├── delist.py           # noindex headers for the dead clusters
│   │   ├── indexnow.py         # submit changed URLs to Bing, Yandex, Seznam, Naver
│   │   └── youtube.py          # parked
│   │
│   ├── aeo/                    # buyer-question panel vs Gemini grounding → citations
│   └── media/                  # parked: TTS, footage, ffmpeg compositing
│
├── vendor/
│   ├── render_articles.py       # the site's own renderer, vendored
│   └── scan_marks.py            # the write-like-me marks scanner, vendored byte-for-byte
├── state/                      # CSV snapshots, committed each run
├── DESIGN.md · SETUP.md · VENDOR.md · LICENSE
```

### The security boundary (principle 3, made concrete)

| Job | Reads open web? | Runs the model? | Holds publish creds? | Secrets loaded |
|---|---|---|---|---|
| `plan` | **yes** (own pages, AEO grounding) | yes | **no** | GSC, Bing, Gemini (`compose`) |
| `apply` | **no** | **no** | **yes** | GitHub PAT (`publishing`) |

`plan` reads untrusted content but cannot publish. `apply` can publish but only ever executes rows already
written to the ledger, and makes no model call of its own — the copy it commits was fixed at compose time.
A prompt injection landing in `plan` has no path to a posting credential.

This is the *only* structural rule left. It matters more now than it did under the gate, not less: there is
no longer a human reading the output before it ships.

---

## 3. The loop

```
  plan  (compose env)                          apply  (publishing env)
  ────────────────────                         ───────────────────────
  inventory + GSC + Bing + URL Inspection
  refresh the duplication corpus from live
  brief: rank actions by est. click gain
      │
      ├── on-page: pages ranked but not clicked
      │     → compose title + meta description
      │     → validate: length, query terms, slop
      │
      ├── on-page: pages on page two, no FAQ
      │     → compose 3 Q&A
      │     → quality gate
      │
      └── new post: demand with no page close enough
            → compose 700-1000 words
            → quality gate: slop, length, truncation,
              duplication vs the live corpus
      │
      ▼
   ledger row (planned) ───────────────────►  read the ledger
   + evidence, + what the gate decided        delist:  noindex the dead clusters
                                              relink:  link orphaned live pages
                                              post:    render → commit md + html + sitemap + index
                                              meta:    rewrite head + structured data
                                              faq:     insert a section before the CTA
                                                   │
                                                   ▼
                                              one atomic commit per action on
                                              intrepidkarthi/dailyvox@main
                                              → Vercel deploys
```

Two kinds of blog page exist, and the fixer handles both. 8 pages have markdown in `content/articles/` and
are rendered by the vendored `render_articles.py`; editing their HTML directly would be reverted on the next
render, so the markdown is the source. The other 134 are committed HTML with no source anywhere in the site
repo — and they earn every impression the blog gets, so `publish/page.py` edits their head metadata in place.

### What stops it

| Control | Where | Effect |
|---|---|---|
| quality gate | `quality/gate.py`, at compose time | P0 blocks outright; 6+ P1 tells block |
| provenance marks | `quality/marks.py` + `vendor/scan_marks.py` | invisible carriers stripped; placeholders block |
| duplication | `quality/plagiarism.py` | ≥28% shingle overlap with a live page blocks |
| empty corpus | `act/plan.py` | no corpus → no new posts at all, since nothing can be checked |
| daily / weekly cap | `act/policy.py` | ≤1 post/day, ≤3/week, counting queued *and* shipped |
| page cooldown | `act/policy.py` | a page edited in the last 30 days is not touched again |
| staleness | `act/apply.py` | a draft composed 14+ days ago is dropped, not shipped |
| path allowlist | `publish/site.py` | a commit outside four path prefixes raises, never sends |
| overwrite guard | `publish/blog.py` | refuses to replace a live page with a "new" post |
| kill switch | `AUTOSEO_PAUSE` or `state/PAUSE` | both halves stop before doing anything |

### Key interfaces

```python
# compose/llm.py
def complete(prompt: str, tier: Tier = Tier.FREE) -> str      # FREE | CHEAP | PREMIUM

# publish/site.py
def commit(files: dict[str, str], message: str) -> str        # one atomic commit, or "" for no-op

# act/ledger.py
def plan(item: Item) -> int
def planned(kind: str | None = None) -> list[Item]
def ship(item_id: int, commit_url: str) -> None
```

---

## 4. Data model (SQLite, snapshotted to committed CSV)

| Table | Purpose |
|---|---|
| `gsc_page_daily` | per page × day: impressions, clicks, position, CTR |
| `gsc_query_daily` | per query × day — the complete view of demand |
| `gsc_page_query` | the page↔query mapping. Lossy by nature; never used to measure volume |
| `url_inventory` | one row per URL, with its cluster and sitemap membership |
| `url_index_status` | per-URL indexed / canonical / coverage state, from URL Inspection |
| `bing_daily` | Bing Webmaster totals |
| `aeo_probe` | per question × engine × run: mentioned, cited |
| `aeo_citation` | every source an answer engine cited — the outreach target list |
| `queue_item` | the ledger: what was decided, the evidence, and what happened |
| `corpus_shingle` | hashed 5-grams of the live site. A cache — never committed, rebuilt each run |
| `run_log` | every command, exit state, and error |

State is committed as sorted CSV, not as the SQLite file. Git merges CSV line by line; committing the binary
cost a rejected push, two unresolvable conflicts, a commit recovered from the reflog, and a silently wiped
16-month backfill. Committing state each run also keeps GitHub's 60-day scheduled-workflow timer alive.

---

## 5. Decision engine

Today: **estimated click gain**, per (page, query). `impressions × (CTR at a plausible position − CTR now)`,
using a standard position→CTR curve. Deliberately crude — it exists to rank actions against each other, not
to forecast traffic. Brand, competitor-internal and irrelevant queries are excluded from acquisition analysis;
including them once produced a confident, wrong recommendation to rewrite `/about`'s title.

Parked: the multi-armed bandit. It needs a feedback loop — publish, wait, measure the same page again — and
that loop only exists now that publishing is automatic. Six weeks of ledger rows paired with position data is
the input it was always waiting for.

---

## 6. Quality gate — plagiarism and AI-slop

**Nothing is composed past this.** It used to decide what reached a human; now it decides what ships. DailyVox
sells architectural integrity; obviously machine-written copy costs more brand credit than the reach is worth.
The verdict is recorded on the ledger row, which is where the reasoning has to live when nobody reads it in
the moment.

The useful realisation: **most of this is deterministic.** Word lists, densities, ratios and fingerprints are
regex and arithmetic — no tokens, no latency, no cost. A model is only needed for the handful of genuine
judgment calls, which keeps the gate free and, more importantly, *auditable*.

### `slop.py` — mechanical, $0
Implements the `write-like-me` rule set:

| Check | Rule | Severity |
|---|---|---|
| **Fingerprints** | `citeturn…`, `oaicite`, `utm_source=chatgpt.com`, unfilled `[Your Name]` / `2026-XX-XX` placeholders | **P0 — hard block.** These are proof, not signal |
| Tier 1 vocabulary | `delve`, `leverage`, `robust`, `testament to`, `seamless`, `game-changer`… — always flag | P1 |
| Tier 2 vocabulary | `harness`, `foster`, `navigate`, `crucial`… — flag at 2+ in one paragraph | P1 |
| Tier 3 vocabulary | `significant`, `innovative`, `compelling`… — flag at ≥3% density | P2 |
| Tier 3 phrases | boilerplate like `the intersection of`, `community-driven` — flag at 2× same, or 3+ distinct | P2 |
| Em dashes | ≤1 per 1,000 words | P1 |
| Hashtag stuffing | 6+ on a short post | P0 on social |
| Type-token ratio | flag under 0.40 on pieces over 200 words | P2 |
| Sentence burstiness | stdev of sentence length; uniform 15–25 words is the strongest structural tell | P1 |
| Structure | heading count vs word count, bold density, bullet-lists of bare noun phrases | P2 |

### `voice.py` — the `karthik` hard-nos, automatic failure
Emoji · "excited to announce" · trailing hashtag block · "In today's fast-moving world" · inspirational
closers · performed emotion ("I was thrilled to…"). Plus profile targets: lead with the claim or the number,
prefer `is`/`has` over "serves as", vary sentence length hard, at most one signature phrase.

Context profiles set strictness per channel — `linkedin` tolerates 2 em-dashes and bold hooks; `blog` is
strict; `technical-blog` exempts `robust`/`comprehensive`/`ecosystem` where they carry real technical meaning.

### `plagiarism.py` — internal matters more than external
- **Internal (the important half).** Shingle the draft (5-grams) and compare against the existing
  1,724-page corpus with MinHash/Jaccard. Self-duplication is precisely what triggers the scaled-content-abuse
  risk the site is already carrying, and it's free to check locally. Block above a similarity threshold.
- **External.** Quote-search 3–5 distinctive n-grams via the SERP API (Bright Data free tier, 5,000/mo).
  A near-exact match on another domain blocks. Free at this volume.

### `gate.py`
`P0 → block` · `P1 ≥ threshold → block` · `P1 below threshold or P2 → warn, recorded on the ledger row`.
A blocked draft returns to `compose/` for one automatic rewrite pass with the gate's own complaints as
instructions; if it fails twice, it's dropped and the slot is logged as skipped rather than filled with
something worse.

**One honest caveat, carried from the skill itself:** these are signals, not proof. Independent audits put
false-positive rates above 60% on non-native English writing. Under the old design that was the argument for
feeding a human card rather than auto-publishing. Autonomously, it cuts the other way — the gate is now
*conservative*, and a false positive costs a skipped slot rather than a bad page. That is the right direction
for the error to run when nobody is reading the output.

---

## 7. LLM strategy

Provider-agnostic by design; swapping is config, not code.

| Tier | Default | Used for | Cost |
|---|---|---|---|
| `FREE` | **Gemini** (AI Studio free tier) | scan, classify, dedup, social copy, AEO probes | **$0** |
| `CHEAP` | OpenRouter `:free` models, else sub-$0.10/Mtok | bulk drafting, fallback when Gemini rate-limits | ~$0 |
| `PREMIUM` | Anthropic (optional) | the occasional long-form blog piece | ~$0.02/post |

**Gemini is the workhorse and it is genuinely free at our volume** — including 5,000 grounded prompts/month,
which covers the entire AEO panel at weekly cadence. `PREMIUM` is opt-in; the system runs fully without it.

---

## 8. Build order

| Phase | Deliverable | Cost |
|---|---|---|
| 0 ✅ | `core/`, `collect/{inventory,gsc,inspect,bing}` → real indexation ratio | $0 |
| 1 ✅ | `quality/` — slop + plagiarism gate. Built *before* anything can publish, not after | $0 |
| 2 ✅ | `publish/blog.py` — render, commit, link from the index | $0 |
| 3 ✅ | `aeo/` — the buyer-question panel and citation extraction | $0 |
| 4 ✅ | `act/` — the autonomous loop: caps, ledger, on-page fixer, direct commits | $0 |
| 5 ✅ | **finish the de-listing** — `delist --apply`, now automatic. Highest ROI available | $0 |
| 6 | `decide/bandit.py` — after ~6 weeks of ledger rows paired with position data | $0 |
| 7 | video and social — parked deliberately, not blocked | $0 |

`quality/` is deliberately ahead of every publishing phase. A gate added after the pipeline works is a gate
that gets bypassed the first time it's inconvenient. Phases 3 and 4 are the ones the removal of the human
gate depended on: the approval was doing work, and it had to be replaced by something before it could go.

### Phase 1 in detail — the de-listing is only half done

The 1,568 sitemap-excluded URLs were removed **on purpose**: they weren't producing outcomes and there was a
reasonable worry about a scaled-content-abuse penalty. That instinct was right.

But removing a URL from a sitemap only withdraws a *discovery hint*. It does not deindex the page, does not
stop crawling, and does not remove it from site-wide quality signals. All 1,568 still return `200`, and
`robots.txt` still allows every crawler. So the intended protection isn't actually in place yet.

To finish it, per cluster:
- **`noindex`** — keeps the page reachable, drops it from the index. Reversible.
- **`410 Gone`** — removes it outright. Faster and unambiguous, but irreversible.

⚠️ **Do not `Disallow` them in robots.txt.** Google has to crawl a page to see a `noindex`, so blocking in
robots.txt *preserves* the index entry rather than removing it. This is the single most common way this
cleanup gets done backwards.

Phase 0's URL Inspection data is what decides it: any cluster still showing `Submitted and indexed` needs
active removal, and any individual `/for/` page with real GSC impressions is a keep-and-relist candidate
rather than a deletion.

---

## 9. Credential storage

**`autoseo` is a public repo, so no secret material is ever committed to it — not even encrypted.**

### Static secrets → GitHub Actions secrets, scoped by Environment
GitHub encrypts them at rest, masks them in logs automatically, and — importantly — does **not** expose them
to workflows triggered by forked PRs. Nearly every credential here is static: the GSC service account, Gemini,
Bing, YouTube's refresh token (once the consent screen is *in production*), Reddit's script app, App Store
Connect.

The security boundary in §2 is **enforced, not documented**, using GitHub **Environments**:

| Environment | Secrets | Used by |
|---|---|---|
| `compose` | `GSC_SERVICE_ACCOUNT_JSON`, `BING_WEBMASTER_API_KEY`, `GEMINI_API_KEY` | the `plan` job |
| `publishing` | `GH_DAILYVOX_TOKEN` (`YT_*` parked) | the `apply` job **only** |

Two environments, not three. An earlier draft split read-only collectors from LLM keys, but that
distinction bought nothing: a Gemini key is not a publishing credential, and its worst case (spend) is
already covered by the free tier. The boundary that carries weight is publishing vs everything else,
and that one is absolute.

A job declares `environment: compose` and simply cannot reference a publishing secret — the boundary is a
GitHub-enforced property rather than a naming convention.

**Neither environment has a required reviewer**, and that is deliberate. One was configured on `publishing`
when every run was meant to pause for a person. It was removed when the gates were: a "required reviewer" on
a daily unattended job is not a safety feature, it is a job that never runs. What protects that credential now
is the allowlist in `publish/site.py` — four path prefixes, checked before any commit is sent — plus the fact
that the job holding the token never reads the open web or calls a model.

### Local development
A gitignored `.env`, generated from `.env.example`. `core/config.py` reads env vars identically in both places,
so there is no "works locally, breaks in CI" gap. `core/log.py` redacts secret-shaped strings as a second line
of defence behind GitHub's own masking.

---

## 10. Non-goals

- Not a multi-tenant product. Single site, single operator.
- Not a content farm. Volume is explicitly *not* the objective — the site already has ~1,723 pages against
  ~60 indexed, and prior evidence says volume backfired.
- No scraping of ChatGPT/Perplexity/AI-Overview UIs. ToS violation, ~5% success rate, asymmetric brand risk.
- No stock-footage-plus-TTS video. YouTube's 16 Jul 2026 policy targets exactly that shape.
