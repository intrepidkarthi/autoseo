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

    for update in _call("getUpdates", limit=100):
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


def edit_card(message_id: int, text: str) -> None:
    """Replace a card's text and drop its buttons, so a decision cannot be made twice."""
    _call("editMessageText", chat_id=chat_id(), message_id=message_id,
          text=text, parse_mode="HTML", disable_web_page_preview=True)


def answer_callback(callback_id: str, text: str = "") -> None:
    """Stops Telegram's spinner on the tapped button."""
    _call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def poll_updates() -> list[dict]:
    """Fetch updates since the last processed one.

    The offset is persisted, so a decision is never acted on twice even if a run is retried — which
    matters when the action on the other side is publishing something.
    """
    offset = _state_get("update_offset")
    params: dict[str, Any] = {"timeout": 0, "limit": 100}
    if offset:
        params["offset"] = int(offset) + 1
    updates = _call("getUpdates", **params)
    if updates:
        _state_set("update_offset", updates[-1]["update_id"])
    return updates


def dump_state() -> str:
    with session() as conn:
        rows = conn.execute("SELECT key, value FROM gate_state").fetchall()
    return json.dumps({r["key"]: r["value"] for r in rows}, indent=2)
