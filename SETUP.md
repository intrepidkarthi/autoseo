# autoseo — Access & credentials checklist

Everything here needs your logins, so only you can do it. Ordered by the phase that needs it — don't create a
credential before it's used.

**Console UIs move labels around. If a menu name doesn't match exactly, look for the nearest equivalent; the
sequence of steps is stable even when the wording drifts.**

---

## Where credentials live

**GitHub → `intrepidkarthi/autoseo` → Settings → Environments** — *not* the flat repo-secrets list.
Create two environments, put each secret in exactly one:

| Environment | Secrets | Used by |
|---|---|---|
| `compose` | `GSC_SERVICE_ACCOUNT_JSON`, `BING_WEBMASTER_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GA4_PROPERTY_ID`, `ASC_*` | `collect.yml`, `plan.yml`, `gate.yml`, `measure.yml` |
| `publishing` | `GH_DAILYVOX_TOKEN`, `GH_SECRETS_TOKEN`, `YT_*`, `IG_*` | `publish.yml` **only** |

**To create one:** repo → Settings → Environments → *New environment* → name it → *Add environment secret*.
Set **`publishing` to require a reviewer (you)** — *Environment protection rules → Required reviewers* — so any
change to that workflow needs explicit approval before it runs with posting credentials.

Why environments and not plain repo secrets: a workflow declares `environment: compose` and then *cannot*
reference a publishing secret. The security boundary becomes GitHub-enforced rather than documented.

> `autoseo` is **public**. Actions secrets are encrypted at rest, masked in logs, and **not** available to
> workflows triggered by forked PRs. **No secret material is ever committed to the repo — not even encrypted.**

---

# PHASE 0 — needed to start (~20 min total)

## 1. `GSC_SERVICE_ACCOUNT_JSON` → environment `compose`

The only fiddly one. Two halves: make a service account in Google Cloud, then grant it access in Search Console.

**A. Create the Cloud project**
1. Go to https://console.cloud.google.com
2. Project dropdown in the top bar → **New Project**
3. Name it `autoseo` → **Create** → make sure it's selected in the dropdown afterwards

**B. Enable the API**
4. Left nav → **APIs & Services → Library**
5. Search **"Google Search Console API"** → click it → **Enable**
   *(While here, also enable **Google Analytics Data API** if you want GA4 in Phase 0.)*

**C. Create the service account**
6. **APIs & Services → Credentials** → **+ Create Credentials** → **Service account**
7. Name: `autoseo-collector` → **Create and continue**
8. **Skip the role step** — leave it blank. Search Console permissions are granted in Search Console, not here.
   → **Done**

**D. Download the JSON key**
9. Click the service account you just made → **Keys** tab
10. **Add Key → Create new key → JSON → Create**
11. A `.json` file downloads. Open it and copy the `client_email` value — you need it next.

**E. Grant it access in Search Console** ← the step people miss
12. Go to https://search.google.com/search-console and select the **getdailyvox.com** property
13. **Settings** (left sidebar) → **Users and permissions**
14. **Add user** → paste the `client_email` → Permission: **Owner**
    - If **Owner** isn't offered in that dropdown, use the **Manage property owners** link on the same page
      (it opens the ownership-verification screen) → **Add an owner** → paste the email.
    - You must already be a verified owner yourself to delegate ownership.

> **Why Owner and not Full:** Search Analytics works fine with Full, but the **URL Inspection API requires
> owner-level access** — and that's the API that finally tells you how many of your 1,723 pages Google has
> actually indexed. That's the entire point of Phase 0.

**F. Store it**
15. Open the downloaded `.json` in a text editor, select all, copy
16. GitHub → `autoseo` → Settings → Environments → **`compose`** → *Add environment secret*
17. Name: `GSC_SERVICE_ACCOUNT_JSON` · Value: the **entire JSON**, braces and all

---

## 2. `GEMINI_API_KEY` → environment `compose` (~2 min, free)

1. Go to https://aistudio.google.com/apikey
2. Sign in with the same Google account
3. **Create API key** → when asked, pick the existing **`autoseo`** Cloud project
   *(keeps quota and usage visible in one place — the free tier applies either way)*
4. Copy it immediately
5. GitHub → Environments → **`compose`** → *Add environment secret* → `GEMINI_API_KEY`

This one key runs the entire system: drafting, classification, dedup, and the whole AEO panel — the free tier
includes 5,000 grounded prompts/month, which covers the panel at weekly cadence outright.

---

## 3. `BING_WEBMASTER_API_KEY` → environment `compose` (~1 min, free)

1. Go to https://www.bing.com/webmasters and sign in
2. If **getdailyvox.com** isn't listed yet, use **Import from Google Search Console** — one click, verifies
   instantly, no DNS or file upload
3. **Gear icon** (top right) → **API Access** → **API Key**
4. Generate → copy
5. GitHub → Environments → **`compose`** → `BING_WEBMASTER_API_KEY`

Worth doing even though it feels minor: Bing's index feeds Copilot and parts of ChatGPT search, so it's an AEO
signal, not just a second-string search engine.

---

## 4. `GA4_PROPERTY_ID` + `ASC_*` → environment `compose` *(optional in Phase 0)*

**GA4** — reuse the same service account:
1. GA4 → **Admin → Property access management** → **+** → add the `client_email` as **Viewer**
2. Enable **Google Analytics Data API** in the `autoseo` Cloud project
3. `GA4_PROPERTY_ID` is the numeric ID under **Admin → Property details**

**App Store Connect** — this is the north-star metric (installs), worth doing early:
1. https://appstoreconnect.apple.com → **Users and Access → Integrations → App Store Connect API**
2. **Team Keys** → **+** → name it `autoseo`, access **Sales and Reports** → Generate
3. Download the **`.p8` file — you can only download it once**
4. Note the **Key ID** and the **Issuer ID** (shown above the key list)
5. Secrets: `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_PRIVATE_KEY` (the full `.p8` contents)

---

# PHASE 2 — Telegram gate (~5 min)

## 5. `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` → environment `compose`

1. Open Telegram, search **@BotFather**, start a chat
2. Send `/newbot`
3. Display name: `AutoSEO` (anything)
4. Username: must be unique and end in `bot` — e.g. `dailyvox_autoseo_bot`
5. BotFather replies with the **token** (`1234567890:AAG...`) → that's `TELEGRAM_BOT_TOKEN`
6. **Now open your new bot and send it `/start`.** Bots cannot message you first — skip this and every card
   silently fails to deliver.
7. Search **@userinfobot**, send it any message → it replies with your numeric **Id** → that's
   `TELEGRAM_CHAT_ID`

No webhook, no public URL, no hosting — we poll `getUpdates` on a `*/20` cron.

---

# PHASE 3 — blog publishing (~5 min)

## 6. `GH_DAILYVOX_TOKEN` → environment `publishing`

1. GitHub → your avatar → **Settings** → **Developer settings** (bottom of left nav)
2. **Personal access tokens → Fine-grained tokens** → **Generate new token**
3. Name: `autoseo → dailyvox PRs` · Expiration: 90 days
4. **Resource owner:** `intrepidkarthi`
5. **Repository access:** *Only select repositories* → **`intrepidkarthi/dailyvox`**
6. **Repository permissions:**
   - **Contents:** Read and write
   - **Pull requests:** Read and write
   - leave everything else at *No access*
7. **Generate token** → copy it now, it's shown once
8. GitHub → `autoseo` → Environments → **`publishing`** → `GH_DAILYVOX_TOKEN`

Scoped to one repo, and the agent opens PRs rather than committing to `main` by policy.

---

# PHASE 4 — social publishing

## 7. `YT_CLIENT_SECRET_JSON` + `YT_TOKEN_JSON` → environment `publishing` (~15 min)

Reuse the `autoseo` Cloud project.

1. **APIs & Services → Library** → **YouTube Data API v3** → **Enable**
2. **APIs & Services → OAuth consent screen** → User type **External** → **Create**
3. Fill App name, User support email, Developer contact → **Save and continue**
4. **Scopes** → *Add or remove scopes* → add `https://www.googleapis.com/auth/youtube.upload`
5. **Test users** → add your own Google account
6. ⚠️ **Back on the OAuth consent screen overview, click `PUBLISH APP` so status reads "In production."**
   Leave it in *Testing* and your refresh token **expires every 7 days** — the automation dies silently a week
   after launch. It may stay **unverified**; the 100-user cap is irrelevant since you're the only user.
7. **Credentials → Create Credentials → OAuth client ID** → Application type **Desktop app** → **Create**
8. **Download JSON** → this is `client_secret.json`
9. Put it in `relay/` and run `python -m autopilot.auth` once — a browser opens, you consent, it writes
   `token.json`
10. Secrets: `YT_CLIENT_SECRET_JSON` and `YT_TOKEN_JSON` (full contents of each file)

*Quota: `videos.insert` has its own bucket of 100 calls/day at 1 unit each — 100 free uploads/day. Any guide
saying "1600 units, so 6 uploads/day" is out of date.*

## 8. `IG_APP_ID` / `IG_APP_SECRET` / `IG_ACCESS_TOKEN` / `IG_USER_ID` → `publishing` (~30 min)

`@getdailyvox` is already a **Creator** account, which is supported and gives the full trending-audio library.

1. https://developers.facebook.com → log in → **My Apps → Create App**
2. Use case: **Other** → App type: **Business** → name it → Create
3. In the app dashboard, find **Instagram** → **Set up**
4. Choose **Instagram API with Instagram Login** *(this variant needs **no Facebook Page**)*
5. Under **API setup with Instagram login**:
   - **Step 1 — Generate access token:** add the `@getdailyvox` account and authorize it
   - Copy **Instagram App ID** → `IG_APP_ID`, **Instagram App Secret** → `IG_APP_SECRET`
   - Generate a token with scopes **`instagram_business_basic`** and
     **`instagram_business_content_publish`**
6. Exchange the short-lived token for a **long-lived** one (60 days) → `IG_ACCESS_TOKEN`
7. Call `GET /me?fields=user_id` with that token → `IG_USER_ID`

⚠️ **No App Review needed.** Standard Access covers publishing to accounts you have a role on. App Review is
only for publishing on behalf of *third parties*, which we never do.

## 9. `GH_SECRETS_TOKEN` → environment `publishing` (~2 min)

The Instagram token expires every 60 days and the refreshed value has to persist — and nothing secret goes
into a public repo. So this token exists purely to rotate that one secret:

1. Same fine-grained token flow as step 6
2. **Repository access:** *Only select repositories* → **`intrepidkarthi/autoseo`**
3. **Repository permissions: Secrets → Read and write.** Nothing else, at all.
4. Store as `GH_SECRETS_TOKEN`

That's its entire scope: it can rotate a secret and do literally nothing else. It holds no posting power.
`publish/instagram.py` refreshes weekly (tokens are refreshable after 24 h, so it never approaches expiry) and
alerts via Telegram if a refresh fails.

## 10. X — **no credential needed** ✅

X is a **manual channel**. autoseo researches, drafts, picks the slot, and sends a copy-paste-ready message to
Telegram with a *Posted / Skipped* button. You paste it into `@getDailyVox`.

No developer account, no card on file, no API cost — this removes the only paid credential in the system.
It's also the better editorial call: founder voice benefits from a human beat, and the link can go anywhere
without the $0.200-per-link-post penalty.

## 11. Reddit — `REDDIT_*` → `compose` (~5 min, metrics only)

1. https://www.reddit.com/prefs/apps → **create another app...**
2. Type: **script** · redirect uri: `http://localhost:8080`
3. Copy the ID under the app name → `REDDIT_CLIENT_ID`, and the **secret** → `REDDIT_CLIENT_SECRET`

⚠️ Posting stays **off**. Per `engagement-playbook.md` the account may be shadow-filtered; this credential is
for reading metrics until the warm-up rules are satisfied.

*Quora has no publishing API — manual only, same card flow as X.*

---

# LLM access

**One free Gemini key runs everything.** The rest are optional.

| Secret | Where | Why | Cost |
|---|---|---|---|
| **`GEMINI_API_KEY`** | https://aistudio.google.com/apikey | **The workhorse** — drafting, classification, dedup, entire AEO panel | **$0** |
| `OPENROUTER_API_KEY` | https://openrouter.ai/keys → *Create key* | Fallback when Gemini rate-limits; one key reaches many free `:free` models | ~$0 |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API keys *(you may already have one)* | **Optional** — occasional long-form blog piece | ~$0.02/post |
| `FAL_KEY` | *(already in `relay/.env`)* | **Optional** — Instagram stills only, never AI video | $0–3/mo |

Set `GEMINI_API_KEY` now and skip the rest. The router is tiered `FREE | CHEAP | PREMIUM`, so a missing key
degrades rather than breaks.

---

## Summary — to start Phase 0

| # | Secret | Env | Time |
|---|---|---|---|
| 1 | `GSC_SERVICE_ACCOUNT_JSON` — service account added as **Owner** in Search Console | `compose` | ~15 min |
| 2 | `GEMINI_API_KEY` | `compose` | ~2 min |
| 3 | `BING_WEBMASTER_API_KEY` | `compose` | ~1 min |

Phase 0 only reads — it writes no content and publishes nothing — so it's safe to run the moment these exist.

### Running cost

| Line | Monthly |
|---|---|
| Hosting, blog PR, YouTube, Instagram, GSC, Bing, GA4, Telegram, IndexNow, LLM, X (manual) | **$0** |
| fal.ai — optional, Instagram stills only | $0–3 |

---

## Two things to decide, not create

1. **Blog PR target** — PRs against `main` on `intrepidkarthi/dailyvox`, or a `content/` branch you merge in
   batches? Default: `main`, one PR per post, you merge.
2. **`/for/` + `/in/` cull method** — `410 Gone` or `noindex`? I lean **410** (faster, unambiguous), but it's
   irreversible, so Phase 0's per-cluster indexation data should settle it. Can wait ~a week.
