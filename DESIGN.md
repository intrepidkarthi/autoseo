# autoseo — Design

Automated blog + social publishing and SEO/AEO measurement for DailyVox.
Runs entirely on GitHub Actions. No server. Target cost ~$1–5/month.

---

## 1. Principles

1. **Statistics decide allocation, the LLM decides craft.** Scheduling is arithmetic — a bandit does it for
   $0, deterministically, auditably. The LLM writes copy and judges freshness. Nothing else.
2. **Nothing publishes without human approval** until a channel earns autonomy per-channel.
3. **A module that reads the open web never holds publishing credentials.** This is the prompt-injection
   firewall and it is enforced by *workflow-level secret scoping*, not by convention.
4. **Vendor, don't depend.** Third-party MIT code is copied in with its header, pinned, and listed in
   `VENDOR.md`. No upstream churn, no submodules.
5. **Every automated decision explains itself.** A card that can't say *why* is a bug.
6. **Free collectors are never gated by budget.** The measurement series must never have a hole.

---

## 2. Module map

```
autoseo/
├── .github/workflows/          # the only schedulers; each loads a DIFFERENT secret set
│   ├── collect.yml             # 06:00 IST  — measurement            [READ-ONLY creds]
│   ├── plan.yml                # 06:30 IST  — decide, draft, notify  [LLM + Telegram send]
│   ├── gate.yml                # */20       — poll approvals         [Telegram only]
│   ├── publish.yml             # hourly     — publish approved       [WRITE creds, no web reads]
│   └── measure.yml             # weekly     — AEO panel + ranks      [READ-ONLY creds]
│
├── autoseo/
│   ├── core/
│   │   ├── config.py           # env → typed settings. Single source of truth.
│   │   ├── db.py               # SQLite connection + schema migrations
│   │   ├── models.py           # dataclasses: Draft, ScheduledPost, Metric, Probe, Decision
│   │   └── log.py              # structured logging; redacts anything secret-shaped
│   │
│   ├── collect/                # ← reads the world. NO write credentials, ever.
│   │   ├── base.py             # Collector protocol
│   │   ├── gsc.py              # Search Analytics API
│   │   ├── inspect.py          # URL Inspection API (2,000/day) → indexed vs not
│   │   ├── bing.py             # Bing Webmaster Tools API
│   │   ├── ga4.py              # GA4 Data API, incl. AI-referrer segmentation
│   │   ├── appstore.py         # App Store Connect — installs (the north star)
│   │   └── platform_stats.py   # pull back per-post engagement for the bandit
│   │
│   ├── decide/                 # ← pure functions. No network, no LLM, no credentials.
│   │   ├── opportunity.py      # GSC gap query → content backlog
│   │   ├── bandit.py           # Thompson sampling over factored arms
│   │   ├── constraints.py      # hard limits; overrides the bandit unconditionally
│   │   └── planner.py          # orchestrates the above → a day_plan
│   │
│   ├── compose/                # ← touches LLM APIs. No publishing credentials.
│   │   ├── llm.py              # provider-agnostic router (tiered: free → cheap → premium)
│   │   ├── dedup.py            # semantic freshness check vs the content log
│   │   ├── blog.py             # → markdown for render_articles.py
│   │   ├── social.py           # → per-platform copy
│   │   └── prompts/            # brand guardrails + per-format templates
│   │
│   ├── quality/                # ← the pre-publish gate. Regex + arithmetic, so $0.
│   │   ├── slop.py             # AI-ism rules, length floor, truncation, stylometry, hard-nos
│   │   ├── plagiarism.py       # shingled 5-grams vs the 1,722-page corpus (hashes only)
│   │   └── gate.py             # P0 blocks; P1 must reach 6
│   │
│   ├── media/
│   │   ├── tts.py              # Kokoro-82M (Apache-2.0), CPU, on-runner
│   │   ├── render.py           # thin wrapper over vendor/mpt
│   │   └── image.py            # HTML→PNG default; fal.ai optional
│   │
│   ├── gate/
│   │   ├── cards.py            # build the self-explaining approval card
│   │   ├── send.py             # push cards to Telegram
│   │   └── poll.py             # getUpdates → resolve approvals (persisted offset)
│   │
│   ├── publish/                # ← holds write credentials. NEVER reads the open web.
│   │   ├── base.py             # Publisher protocol + registry + DeliveryMode
│   │   ├── manual.py           # DeliveryMode.MANUAL — copy-paste-ready card to Telegram
│   │   ├── blog.py             # GitHub Contents API → PR on intrepidkarthi/dailyvox
│   │   ├── youtube.py          # adapted from relay/autopilot/
│   │   ├── instagram.py        # Instagram API with Instagram Login
│   │   └── reddit.py           # credential for metrics first; posting stays off
│   │
│   ├── aeo/
│   │   ├── panel.yaml          # ~50 buyer-intent prompts
│   │   └── probe.py            # Gemini grounding (free tier) → citation extraction
│   │
│   └── budget/
│       └── ledger.py           # spend accounting + tiered degradation
│
├── worker/                     # Cloudflare worker: Telegram webhook -> repository_dispatch
├── vendor/mpt/                 # vendored MoneyPrinterTurbo modules (MIT, headers intact)
├── state/                      # SQLite + JSON snapshots, committed each run
├── DESIGN.md · SETUP.md · VENDOR.md · LICENSE
```

### The security boundary (principle 3, made concrete)

| Workflow | Reads open web? | Holds publish creds? | Secrets loaded |
|---|---|---|---|
| `collect.yml` | no (APIs only) | **no** | GSC, Bing, GA4, App Store (`compose`) |
| `plan.yml` | **yes** (research/scan) | **no** | LLM keys, Telegram bot token |
| `gate.yml` | no | no | Telegram bot token |
| `publish.yml` | **no** | **yes** | GitHub PAT, YouTube, Instagram |
| `measure.yml` | yes (AEO probes) | **no** | Gemini, SERP (`compose`) |

`plan.yml` reads untrusted content but cannot publish. `publish.yml` can publish but only ever consumes rows
already written to SQLite and approved by a human. A prompt injection landing in `plan.yml` has no path to a
posting credential.

---

## 3. Key interfaces

Three protocols keep the system extensible. Adding a platform is one file plus a registry line.

```python
# collect/base.py
class Collector(Protocol):
    name: str
    def collect(self, since: date) -> list[Metric]: ...

# publish/base.py
class DeliveryMode(Enum):
    API    = "api"       # autoseo posts it
    MANUAL = "manual"    # autoseo delivers copy-paste-ready text to Telegram; you post it

class Publisher(Protocol):
    platform: str
    mode: DeliveryMode
    def validate(self, item: ScheduledPost) -> None:   # raise before spending anything
    def publish(self, item: ScheduledPost) -> PublishResult: ...
    def fetch_stats(self, external_id: str) -> Metric | None: ...   # closes the bandit loop

# compose/llm.py
class LLMProvider(Protocol):
    def complete(self, prompt: str, tier: Tier) -> Completion: ...
```

`Tier` is `FREE | CHEAP | PREMIUM`. The router picks a provider per tier, and the budget governor can force
everything down a tier without any code change.

### Manual channels are first-class, not a fallback

Some channels are better off human-executed — X (the API now costs money and founder voice benefits from a
human beat), Quora (no publishing API exists), Reddit (account may be shadow-filtered; posting stays off until
warm-up rules are met). For these, `DeliveryMode.MANUAL` means **everything upstream is identical** — the
bandit picks the slot, constraints apply, the LLM drafts, the card explains itself. Only the last step differs:
instead of an API call, Telegram gets a **copy-paste-ready message** and a *Posted / Skipped* button.

Two requirements that fall out of this:
- **Plain text, no leading markdown characters.** `CONTENT-ENGINE.md` is explicit that Karthik strips those by
  hand otherwise. Manual cards render as literal paste-ready text, not Telegram markdown.
- **Feedback without an API.** Without X API access there is no read path for engagement, so manual channels
  learn from *Posted vs Skipped* (a strong, free signal) plus an optional 48-hour "how did it do?" prompt with
  three buckets — flopped / normal / good. Coarse, but enough for a bandit.

---

## 4. Data model (SQLite, committed to the repo)

| Table | Purpose |
|---|---|
| `gsc_daily` | query × page × impressions, clicks, position, CTR |
| `url_index_status` | per-URL indexed / canonical / rich-result state |
| `opportunity_queries` | rising impressions, position 8–30, no dedicated page → the backlog |
| `platform_metrics` | per-post engagement, pulled back after publish |
| `day_plan` | one row per decision: platform, format, pillar, slot, **evidence**, est_cost |
| `drafts` | generated copy awaiting approval |
| `post_queue` | approval state: `policy`, `approved_by`, `approved_at`, `rejected_reason` |
| `post_log` | what actually went out, with external IDs |
| `bandit_arms` | Beta posteriors (α, β) per arm |
| `aeo_probe` | per prompt × engine × run: mentioned, cited, rank, cited_urls |
| `spend_ledger` | provider, operation, units, usd, ts |

Committing state back each run gives free persistence, a git-history audit log, and it keeps GitHub's
60-day scheduled-workflow timer alive.

---

## 5. Decision engine

**Factored arms** (a full cross-product would overfit at this data volume):
`platform×format` · `time_slot` · `pillar` — scored separately with Beta posteriors, then combined.

**Reward** = engagement normalised *within* platform, with App Store installs as a slow global multiplier.
**Telegram rejections are negative reward** — in the first months this is the highest-quality signal available.

**Cold start:** priors are seeded from `marketing/CONTENT-ENGINE.md` (IST time windows, "Reels >> static",
"links in body hurt reach"). The engine reproduces the existing playbook and diverges only on evidence.
~20% of slots reserved for exploration.

**Constraints override the bandit unconditionally:** platform rate/cost caps; no repeated pillar consecutively
per platform; Reddit sub-queue and shadow-filter warm-up rules; and **throttle-down** — if recent posts
underperform a platform's own baseline, reduce frequency there. "Post nothing today" is a valid output.

---

## 6. Quality gate — plagiarism and AI-slop

**No draft reaches the Telegram card without passing.** DailyVox sells architectural integrity; obviously
machine-written copy costs more brand credit than the reach is worth. Scores ride along on every card that
does pass, so the human gate sees them.

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
`P0 → block` · `P1 ≥ threshold → block` · `P1 below threshold or P2 → warn, card shows the flags`.
A blocked draft returns to `compose/` for one automatic rewrite pass; if it fails twice, it's dropped and the
slot is logged as skipped rather than filled with something worse.

**One honest caveat, carried from the skill itself:** these are signals, not proof. Independent audits put
false-positive rates above 60% on non-native English writing. That's exactly why the gate feeds a *human*
approval card rather than auto-publishing on a green score.

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
| 1 | **finish the de-listing** — see below. No code, highest ROI available | $0 |
| 2 ✅ | `quality/` — slop + plagiarism gate. Built *before* anything can publish, not after | $0 |
| 3 ✅ | `gate/` — Telegram cards, approval state | $0 |
| 4 ✅ | `publish/blog.py` — PR to dailyvox, IndexNow ping | $0 |
| 5 | `publish/{youtube,instagram,manual}.py` + scheduler v1 (priors + constraints) | $0 |
| 6 | `decide/bandit.py` — turn on after ~4–6 weeks of data; back-test before trusting | $0 |
| 7 | `media/` + `vendor/mpt` — video from real screen recordings | $0 |
| 8 | `aeo/` — the prompt panel | $0 |

`quality/` is deliberately ahead of every publishing phase. A gate added after the pipeline works is a gate
that gets bypassed the first time it's inconvenient.

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
| `compose` | `GSC_SERVICE_ACCOUNT_JSON`, `BING_WEBMASTER_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GA4_PROPERTY_ID`, `ASC_*` | `collect.yml`, `plan.yml`, `gate.yml`, `measure.yml` |
| `publishing` | `GH_DAILYVOX_TOKEN`, `GH_SECRETS_TOKEN`, `YT_*`, `IG_*` | `publish.yml` **only** |

Two environments, not three. An earlier draft split read-only collectors from LLM keys, but that
distinction bought nothing: a Gemini key is not a publishing credential, and its worst case (spend) is
already covered by the budget governor. The boundary that carries weight is publishing vs everything else,
and that one is absolute.

A workflow declares `environment: read` and simply cannot reference a publishing secret — the boundary becomes
a GitHub-enforced property rather than a naming convention. `publishing` also gets a **required reviewer**
(you), so any change to that workflow needs an explicit approval before it can run with those secrets.

### The one rotating credential — Instagram
Everything else is static. The Instagram long-lived token expires every 60 days and must be *exchanged* for a
new value, which has to persist somewhere. Three options, and the tradeoff is real:

| Option | Verdict |
|---|---|
| **Write back to the Actions secret** via a fine-grained PAT scoped to `autoseo` with `secrets: write` only | ✅ **Chosen.** Nothing secret touches the repo. The PAT is narrow and holds no posting power of its own |
| Commit the token encrypted (SOPS/age) into `state/` | ❌ Ciphertext in a *public* repo, preserved in git history forever. Wrong story for a privacy-branded project |
| Refresh by hand every 60 days | ❌ Will be forgotten; this is the most common way IG automation dies |

`publish/instagram.py` refreshes weekly (tokens are refreshable after 24 h, so it never gets close to expiry)
and alerts via Telegram if a refresh fails.

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
