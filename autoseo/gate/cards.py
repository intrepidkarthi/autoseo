"""Build the approval card, and process the taps that come back.

The card always states its reasoning. That is the difference between an approval and a reflex: if
the system cannot say why it picked this thing, at this time, for this channel, you should not be
approving it — and you would have no way to correct the logic that produced it.

Rejections are training data. Every Reject is recorded against the item so the decision layer can
later learn preference, which in the early months is a better signal than engagement, because
engagement volume here is tiny while your judgement is available every day.
"""

from __future__ import annotations

import html

from autoseo.core.log import get_logger

from . import client, queue
from .queue import Item, Status

log = get_logger(__name__)

BUTTONS = [("Approve", "ok"), ("Reject", "no"), ("Snooze", "zz")]
MANUAL_BUTTONS = [("Posted", "done"), ("Skipped", "skip")]


def render(item: Item) -> str:
    """HTML card. Telegram's HTML mode is far more forgiving than its Markdown mode, which breaks on
    stray underscores and brackets — common in URLs and draft copy."""
    esc = html.escape
    lines = [
        f"<b>{esc(item.title)}</b>",
        f"<i>{esc(item.channel)} · {esc(item.kind)}</i>",
        "",
    ]
    if item.channel == "blog":
        # The full text was already sent above; repeating it here would just be noise.
        words = len(item.meta.get("markdown", item.body).split())
        lines.append(f"Full draft is above — {words} words.")
        if q := item.meta.get("query"):
            lines.append(f"Targeting: <code>{esc(q)}</code>")
    else:
        lines.append(esc(item.body[:2500]))
    if item.rationale:
        lines += ["", f"<b>Why:</b> {esc(item.rationale)}"]
    if item.meta.get("url"):
        lines += ["", esc(item.meta["url"])]
    return "\n".join(lines)


def send_pending() -> int:
    """Push every queued item that has not been shown yet."""
    sent = 0
    for item in queue.pending_unsent():
        buttons = MANUAL_BUTTONS if item.channel == "manual" else BUTTONS
        try:
            if item.channel == "manual":
                # Copy-paste channels get the draft as its own plain-text message first, so it can
                # be copied cleanly without the card's formatting coming with it.
                client.send_plain(item.body)
            elif item.channel == "blog":
                # An article cannot be reviewed inside a card — Telegram caps a message at 4096
                # characters and the card template truncates well before that. Approving prose you
                # can only half-read defeats the point of the gate. Send the whole thing: as a file
                # for proper reading, and inline in chunks so it is visible without downloading.
                full = item.meta.get("markdown", item.body)
                client.send_document(f"{item.meta.get('slug', 'draft')}.md", full,
                                     caption=f"{item.title} — {len(full.split())} words")
                client.send_long(full)
            message_id = client.send_card(render(item), buttons)
        except RuntimeError as exc:
            log.warning("could not send item %s: %s", item.id, exc)
            continue
        queue.mark_sent(item.id, message_id)
        sent += 1
    if sent:
        log.info("sent %d card(s)", sent)
    return sent


DECISIONS = {
    "ok": (Status.APPROVED, "Approved"),
    "no": (Status.REJECTED, "Rejected"),
    "zz": (Status.SNOOZED, "Snoozed"),
    "done": (Status.POSTED, "Marked posted"),
    "skip": (Status.SKIPPED, "Skipped"),
}


def process_one(payload: str) -> int:
    """Handle a single update delivered by the Cloudflare worker.

    Setting a Telegram webhook disables getUpdates entirely (409), so once the worker is live this
    is the only path that sees callbacks. It deliberately shares dedupe and decision recording with
    the polling path, so a decision cannot be double-applied if Telegram retries the webhook.
    """
    import json as _json

    try:
        update = _json.loads(payload)
    except ValueError as exc:
        log.error("could not parse dispatched update: %s", exc)
        return 0
    if "update" in update:          # the worker nests it under client_payload.update
        update = update["update"]
    return _handle([update])


def process_updates() -> int:
    """Resolve button taps into decisions. Idempotent: the update offset is persisted, so a retried
    run cannot act on the same tap twice."""
    return _handle(client.poll_updates())


def _handle(updates: list[dict]) -> int:
    handled = 0
    seen_ids: list[int] = []
    already = client.already_seen({u.get("update_id") for u in updates})
    for update in updates:
        uid = update.get("update_id")
        if uid in already:
            log.info("update %s already handled — skipping", uid)
            continue
        seen_ids.append(uid)
        cb = update.get("callback_query")
        if not cb:
            continue
        data = cb.get("data", "")
        message_id = (cb.get("message") or {}).get("message_id")
        if data not in DECISIONS or not message_id:
            continue

        status, label = DECISIONS[data]
        with_item = _item_for_message(message_id)
        if not with_item:
            # Happens when a card was delivered but its queue row never got committed — a run can
            # send the message and then fail at the commit step. The card is then permanently
            # un-approvable, and silence here made that look like a broken button for hours.
            log.warning(
                "no queued item for message_id=%s — the card was sent but its row was never "
                "committed, so this tap cannot be honoured", message_id,
            )
            client.answer_callback(cb["id"], "That card is orphaned — a fresh one will follow")
            continue

        # Record the decision FIRST and never let anything cosmetic undo it.
        queue.decide(with_item.id, status)

        # Everything below is presentation. A callback query expires within seconds, and this gate
        # polls on a cron that GitHub runs roughly hourly — so answerCallbackQuery essentially
        # always fails with "query is too old". That used to raise, which killed the run *after*
        # the decision was recorded but *before* the state was snapshotted and committed, so every
        # approval was silently lost. Presentation failures must never cost a decision.
        client.answer_callback(cb["id"], label)
        try:
            client.edit_card(message_id, render(with_item) + f"\n\n<b>— {label}</b>")
        except RuntimeError as exc:
            log.warning("could not update card %s: %s", message_id, exc)
        handled += 1
        log.info("item %s -> %s", with_item.id, status)

    # Confirm only what we actually looked at, and only after looking at it.
    client.confirm(seen_ids)
    return handled


def _item_for_message(message_id: int) -> Item | None:
    from autoseo.core.db import session

    with session() as conn:
        row = conn.execute(
            "SELECT id FROM queue_item WHERE message_id = ?", (message_id,)
        ).fetchone()
    return queue.get(row["id"]) if row else None
