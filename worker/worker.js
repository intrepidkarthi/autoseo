/**
 * Telegram webhook -> GitHub repository_dispatch.
 *
 * Why this exists: the gate previously polled getUpdates on a cron. GitHub runs a `*​/20` schedule
 * roughly hourly, and Telegram callback queries expire within seconds — so by the time a run saw a
 * tap, answering it always failed, and approvals sat unacknowledged for up to an hour.
 *
 * This worker fixes both halves:
 *   1. It answers the callback IMMEDIATELY, inside the expiry window, so the button stops spinning
 *      and shows a confirmation the moment you tap it.
 *   2. It forwards the update to GitHub as a repository_dispatch, which starts the gate workflow at
 *      once instead of waiting for the next cron tick.
 *
 * Note that setting a webhook DISABLES getUpdates (Telegram returns 409). That is why the payload
 * is forwarded rather than just pinging the workflow — after this is live, the workflow can no
 * longer fetch the update itself.
 *
 * Deploy: see worker/README.md
 */

const DECISION_LABEL = {
  ok: "Approved",
  no: "Rejected",
  zz: "Snoozed",
  done: "Marked posted",
  skip: "Skipped",
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    /**
     * GET /setup — register this worker as the Telegram webhook, using the credentials this worker
     * already holds. Nobody has to copy a bot token or a webhook secret anywhere, which is the
     * whole point: those values live in Cloudflare and are write-only, so moving them by hand means
     * pasting them somewhere they should not be.
     *
     * Deliberately unauthenticated, because it is safe to be. The target URL is derived from this
     * request's own origin, never from user input, so the only thing a stranger can accomplish is
     * re-pointing the bot at this same worker — which is the correct state. It cannot be used to
     * redirect the bot elsewhere.
     */
    if (request.method === "GET" && url.pathname === "/setup") {
      const res = await fetch(
        `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/setWebhook`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            url: url.origin,
            secret_token: env.WEBHOOK_SECRET,
            allowed_updates: ["callback_query", "message"],
          }),
        },
      );
      const body = await res.json();
      // Telegram's reply carries no credentials, so it is safe to surface verbatim.
      return Response.json({ target: url.origin, telegram: body });
    }

    /** GET /status — webhook health, with the token stripped from Telegram's reply. */
    if (request.method === "GET" && url.pathname === "/status") {
      const res = await fetch(
        `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getWebhookInfo`,
      );
      const body = await res.json();
      return Response.json(body);
    }

    if (request.method !== "POST") {
      return new Response("autoseo gate webhook", { status: 200 });
    }

    // Telegram echoes the secret configured via setWebhook. Without this check the endpoint is a
    // public trigger for the workflow, and anyone who guesses the URL can forge an approval.
    const presented = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.WEBHOOK_SECRET || presented !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad json", { status: 400 });
    }

    // Acknowledge first, and never let it block the dispatch. This is the entire reason the worker
    // exists — it runs within the seconds-long window where the query id is still valid.
    const cb = update.callback_query;
    if (cb?.id) {
      const label = DECISION_LABEL[cb.data] ?? "Received";
      try {
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ callback_query_id: cb.id, text: label }),
        });
      } catch (err) {
        console.log("ack failed (non-fatal):", err.message);
      }
    }

    // Forward to GitHub. The gate records the decision from this payload; it can no longer poll.
    const res = await fetch(
      `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`,
      {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GITHUB_TOKEN}`,
          accept: "application/vnd.github+json",
          "content-type": "application/json",
          "user-agent": "autoseo-gate-worker",
        },
        body: JSON.stringify({
          event_type: "telegram_update",
          client_payload: { update },
        }),
      },
    );

    if (!res.ok) {
      // Return non-2xx so Telegram retries. Losing a decision is the worst failure here, and a
      // duplicate is harmless — the gate dedupes on update_id.
      const body = await res.text();
      console.log("dispatch failed", res.status, body.slice(0, 200));
      return new Response("dispatch failed", { status: 500 });
    }

    return new Response("ok", { status: 200 });
  },
};
