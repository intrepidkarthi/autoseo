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
        esc(item.body[:2500]),
    ]
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


def process_updates() -> int:
    """Resolve button taps into decisions. Idempotent: the update offset is persisted, so a retried
    run cannot act on the same tap twice."""
    handled = 0
    seen_ids: list[int] = []
    for update in client.poll_updates():
        seen_ids.append(update["update_id"])
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
            client.answer_callback(cb["id"], "That item is no longer in the queue")
            continue

        queue.decide(with_item.id, status)
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
