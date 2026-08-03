# Instant approvals — Cloudflare Worker webhook

Without this, the gate polls `getUpdates` on a cron. GitHub honours a `*/20` schedule roughly
hourly, and Telegram callback queries expire within **seconds** — so a tap sat unacknowledged for
up to an hour and the ack always failed as "query is too old".

The worker fixes both halves:

1. **Answers the callback immediately**, inside the expiry window, so the button stops spinning and
   confirms the moment you tap it.
2. **Dispatches to GitHub at once** via `repository_dispatch`, so the gate records the decision in
   seconds rather than at the next cron tick.

Cost: **$0.** Cloudflare's free tier is 100,000 requests/day; this uses a handful.

> **Setting a webhook disables `getUpdates`** — Telegram returns 409 for it. That is why the worker
> forwards the whole update rather than just pinging the workflow: after this is live, the workflow
> can no longer fetch updates itself. The `gate` cron stays as a safety net for *sending* queued
> cards, which does not need polling.

---

## 1. Create the GitHub token the worker will use

GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate new token

- Repository access: **Only select repositories** → `intrepidkarthi/autoseo`
- Permissions → **Contents: Read and write** (this is what `repository_dispatch` requires)
- Nothing else

Copy it. This token can trigger workflows in one repo and do nothing else.

## 2. Pick a webhook secret

Any random string. Telegram echoes it back on every request and the worker rejects anything else —
without it, the URL is a public trigger and anyone who guesses it can forge an approval.

```bash
openssl rand -hex 24
```

## 3. Deploy

```bash
npm install -g wrangler
cd worker
wrangler login
wrangler deploy
```

`wrangler deploy` prints the worker URL, e.g. `https://autoseo-gate.<subdomain>.workers.dev`.

Then set the three secrets:

```bash
wrangler secret put TELEGRAM_BOT_TOKEN   # the BotFather token
wrangler secret put GITHUB_TOKEN         # from step 1
wrangler secret put WEBHOOK_SECRET       # from step 2
```

`GITHUB_REPO` is already set in `wrangler.toml` as a plain var — it is not secret.

## 4. Point Telegram at it

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H 'content-type: application/json' \
  -d '{
        "url": "https://autoseo-gate.<subdomain>.workers.dev",
        "secret_token": "<WEBHOOK_SECRET>",
        "allowed_updates": ["callback_query", "message"]
      }'
```

Expect `{"ok":true,"result":true,"description":"Webhook was set"}`.

Verify:

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo" | python3 -m json.tool
```

`pending_update_count` should be 0 and `last_error_message` absent. If you see
`Wrong response from the webhook: 403`, the secret does not match.

## 5. Test

Send a card and tap it:

```bash
gh workflow run gate.yml --repo intrepidkarthi/autoseo -f test_card=true
```

The button should confirm instantly. Within a few seconds a `repository_dispatch` run appears in
Actions and `state/queue_item.csv` shows the decision.

---

## Turning it off

```bash
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"
```

`getUpdates` starts working again immediately and the cron resumes collecting decisions. Nothing in
the repo needs changing — the gate supports both paths and shares dedupe between them.

## Notes

- **Duplicates are expected and safe.** Telegram retries a webhook until it gets a 2xx, and the
  worker deliberately returns 500 on a failed dispatch so a decision is retried rather than lost.
  The gate dedupes on `update_id` via the `gate_seen` table, so a replay is a no-op.
- **The worker holds the bot token.** It needs it to answer callbacks in time. It cannot post
  content — the gate does that, and the gate's credentials live in GitHub.
- **Free tier is ample.** A tap is one request; 100,000/day is the limit.
