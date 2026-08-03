"""Telegram Bot API client.

Long-polling rather than a webhook, because a webhook needs a public HTTPS endpoint and this whole
system exists to avoid running a server. `getUpdates` on a cron costs nothing and gives approval
latency of at most one poll interval, which is irrelevant for content scheduled hours ahead.

The chat ID is discovered rather than configured. A bot cannot message someone who has not messaged
it first, so a `/start` always exists by the time this runs — reading the ID from that message
removes a manual copy-paste step and the silent-failure mode where a mistyped ID means cards vanish
with no error.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx

from autoseo.core.config import ConfigError, settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = httpx.Timeout(45.0)


def _call(method: str, **params: Any) -> dict:
    if not settings.telegram_bot_token:
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather, then put the token in "
            "GitHub -> autoseo -> Settings -> Environments -> compose."
        )
    resp = httpx.post(
        API.format(token=settings.telegram_bot_token, method=method),
        json=params, timeout=TIMEOUT,
    )
    payload = resp.json() if resp.content else {}
    if not payload.get("ok"):
        # Telegram puts the real reason in `description`; the HTTP status alone says nothing useful.
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', resp.text[:200])}")
    return payload.get("result", {})


def _state_get(key: str) -> str | None:
    with session() as conn:
        row = conn.execute("SELECT value FROM gate_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _state_set(key: str, value: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO gate_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def chat_id() -> str:
    """Configured ID if present, otherwise the chat of whoever messaged the bot first."""
    if settings.telegram_chat_id:
        return settings.telegram_chat_id
    cached = _state_get("chat_id")
    if cached:
        return cached

    try:
        updates = _call("getUpdates", limit=100)
    except RuntimeError as exc:
        if "webhook is active" in str(exc):
            # Discovery is unavailable in webhook mode. It has normally already happened and been
            # cached, but a fresh checkout with no cached value needs the id supplied explicitly.
            raise ConfigError(
                "No cached chat id, and discovery needs getUpdates which the active webhook "
                "disables. Set TELEGRAM_CHAT_ID in the compose environment (message @userinfobot "
                "for your numeric id), or delete the webhook to rediscover."
            ) from exc
        raise

    for update in updates:
        message = update.get("message") or update.get("callback_query", {}).get("message") or {}
        cid = str((message.get("chat") or {}).get("id", ""))
        if cid:
            _state_set("chat_id", cid)
            log.info("Discovered chat id from an existing message")
            return cid

    raise ConfigError(
        "No chat id. Open your bot in Telegram and send it /start — a bot cannot message "
        "someone who has not messaged it first."
    )


def send_card(text: str, buttons: list[tuple[str, str]]) -> int:
    """Send one approval card. Returns the message id so the card can be edited in place later."""
    keyboard = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
    result = _call(
        "sendMessage",
        chat_id=chat_id(),
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )
    return int(result["message_id"])


def send_plain(text: str) -> int:
    """Plain text, no markup. Used for copy-paste drafts, where any formatting is damage —
    CONTENT-ENGINE.md is explicit that stray markdown characters get hand-stripped otherwise."""
    result = _call("sendMessage", chat_id=chat_id(), text=text, disable_web_page_preview=True)
    return int(result["message_id"])


# Telegram hard-caps a message at 4096 characters. An article silently truncated at that boundary
# is worse than no preview: you would be approving prose you cannot see.
MAX_MESSAGE = 3900


def send_long(text: str) -> None:
    """Send text of any length as consecutive plain messages, split on paragraph boundaries."""
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > MAX_MESSAGE and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)

    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"[{i}/{total}]\n" if total > 1 else ""
        _call("sendMessage", chat_id=chat_id(), text=prefix + chunk,
              disable_web_page_preview=True)


def send_document(filename: str, content: str, caption: str = "") -> int:
    """Upload text as a file, so a long draft can be read properly rather than skimmed in chunks."""
    files = {"document": (filename, content.encode("utf-8"), "text/markdown")}
    data = {"chat_id": chat_id(), "caption": caption[:1000]}
    resp = httpx.post(
        API.format(token=settings.telegram_bot_token, method="sendDocument"),
        data=data, files=files, timeout=TIMEOUT,
    )
    payload = resp.json() if resp.content else {}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendDocument failed: {payload.get('description', '')}")
    return int(payload["result"]["message_id"])


def send_video(path, caption: str = "") -> int:
    """Upload the rendered file itself. Bots can send up to 50 MB, and a Short is ~10 MB.

    Approving a video you cannot watch is the same mistake as approving an article you can only
    half-read: the gate stops being a judgement and becomes a reflex.
    """
    with open(path, "rb") as fh:
        resp = httpx.post(
            API.format(token=settings.telegram_bot_token, method="sendVideo"),
            data={"chat_id": chat_id(), "caption": caption[:1000], "supports_streaming": "true"},
            files={"video": (getattr(path, "name", "short.mp4"), fh, "video/mp4")},
            timeout=httpx.Timeout(300.0),
        )
    payload = resp.json() if resp.content else {}
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendVideo failed: {payload.get('description', '')}")
    return int(payload["result"]["message_id"])


def edit_card(message_id: int, text: str) -> None:
    """Replace a card's text and drop its buttons, so a decision cannot be made twice."""
    _call("editMessageText", chat_id=chat_id(), message_id=message_id,
          text=text, parse_mode="HTML", disable_web_page_preview=True)


def answer_callback(callback_id: str, text: str = "") -> None:
    """Stop Telegram's spinner on the tapped button. Never raises.

    Callback queries expire within seconds and this gate polls on an hourly cron, so this call
    essentially always fails with "query is too old". It is purely cosmetic — the decision is
    already recorded by the time it runs — but when it raised it killed the run before state was
    committed, and every approval was lost. Swallowing here rather than at each call site, because
    there is no caller for whom a failed spinner should be fatal.
    """
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id, text=text)
    except RuntimeError as exc:
        log.info("callback ack skipped (expected on a cron): %s", exc)


def poll_updates() -> list[dict]:
    """Return updates we have not acted on yet.

    Read WITHOUT an offset on purpose. Telegram's offset is documented as the way to page through
    updates, but in production a call with offset=N returned zero while the same call without an
    offset returned two updates with ids greater than N — and those two approvals were then
    consumed and lost. Since the payload here is a human decision, silently dropping one is the
    worst possible failure, so the offset is no longer trusted as the read cursor.

    Instead: read everything Telegram still holds, and dedupe against gate_seen. Confirmation (which
    lets Telegram drop them) happens in `confirm()`, only after the caller has actually processed
    them.
    """
    try:
        updates = _call("getUpdates", limit=100)
    except RuntimeError as exc:
        if "webhook is active" in str(exc):
            # Expected and correct once the Cloudflare worker is registered: Telegram refuses
            # getUpdates while a webhook exists. Decisions arrive by repository_dispatch instead,
            # so there is nothing to poll and this is not an error.
            log.info("webhook mode — decisions arrive by dispatch, not polling")
            return []
        raise
    with session() as conn:
        seen = {r["update_id"] for r in conn.execute("SELECT update_id FROM gate_seen")}
    fresh = [u for u in updates if u.get("update_id") not in seen]
    log.info("poll: %d held by telegram, %d already handled, %d fresh %s",
             len(updates), len(updates) - len(fresh), len(fresh),
             [u.get("update_id") for u in fresh])
    return fresh


def already_seen(update_ids: set) -> set:
    """Which of these have we acted on before? Telegram retries a webhook until it gets a 2xx, so
    the same approval can legitimately arrive several times."""
    ids = {int(i) for i in update_ids if i is not None}
    if not ids:
        return set()
    with session() as conn:
        rows = conn.execute(
            f"SELECT update_id FROM gate_seen WHERE update_id IN ({','.join('?' * len(ids))})",
            tuple(ids),
        ).fetchall()
    return {r["update_id"] for r in rows}


def confirm(update_ids: list[int]) -> None:
    """Record ids as handled, then let Telegram release them.

    Recording happens first. If the confirming call fails, the worst case is that Telegram replays
    an update we have already recorded — which dedupe absorbs. The reverse order could lose one.
    """
    if not update_ids:
        return
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with session() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO gate_seen(update_id, ts) VALUES (?, ?)",
            [(int(i), now) for i in update_ids],
        )
    try:
        _call("getUpdates", offset=max(update_ids) + 1, limit=1)
    except RuntimeError as exc:
        # Expected once a webhook is registered: Telegram returns 409 for getUpdates. Nothing to
        # confirm in that mode — the webhook already delivered it.
        log.info("skipping getUpdates confirm (normal in webhook mode): %s", exc)


def dump_state() -> str:
    with session() as conn:
        rows = conn.execute("SELECT key, value FROM gate_state").fetchall()
    return json.dumps({r["key"]: r["value"] for r in rows}, indent=2)
