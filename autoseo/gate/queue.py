"""The approval queue: nothing reaches a channel without a human decision recorded here.

Every item carries a `policy`. `gated` (the default) always asks. Per-channel rules can later flip
an item to `auto`, but only after that channel has a clean approval record — the field exists from
day one so autonomy is a config change rather than a rewrite.

Items also carry `rationale`. A card that cannot explain why the system chose it is a bug, because
an approval given without a reason is not really an approval.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import StrEnum

from autoseo.core.db import session


class Status(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SNOOZED = "snoozed"
    POSTED = "posted"
    SKIPPED = "skipped"
    FAILED = "failed"


class Policy(StrEnum):
    GATED = "gated"
    AUTO = "auto"


@dataclass
class Item:
    kind: str            # outreach | blog | social | test
    channel: str         # manual | blog | youtube | instagram
    title: str
    body: str
    rationale: str
    policy: str = Policy.GATED
    meta: dict = field(default_factory=dict)
    id: int | None = None
    status: str = Status.PENDING
    message_id: int | None = None


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def add(item: Item) -> int:
    with session() as conn:
        cur = conn.execute(
            """INSERT INTO queue_item(created, kind, channel, title, body, rationale,
                                      policy, meta, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now(), item.kind, item.channel, item.title, item.body, item.rationale,
             item.policy, json.dumps(item.meta), Status.PENDING),
        )
        return int(cur.lastrowid)


def pending_unsent() -> list[Item]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_item WHERE status = ? AND message_id IS NULL ORDER BY id",
            (Status.PENDING,),
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def approved_unposted() -> list[Item]:
    with session() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_item WHERE status = ? ORDER BY id", (Status.APPROVED,)
        ).fetchall()
    return [_row_to_item(r) for r in rows]


def get(item_id: int) -> Item | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM queue_item WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def mark_sent(item_id: int, message_id: int) -> None:
    with session() as conn:
        conn.execute("UPDATE queue_item SET message_id = ? WHERE id = ?", (message_id, item_id))


def decide(item_id: int, status: Status, by: str = "telegram") -> None:
    with session() as conn:
        conn.execute(
            "UPDATE queue_item SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, _now(), by, item_id),
        )


def _row_to_item(r) -> Item:
    return Item(
        id=r["id"], kind=r["kind"], channel=r["channel"], title=r["title"], body=r["body"],
        rationale=r["rationale"], policy=r["policy"], status=r["status"],
        message_id=r["message_id"], meta=json.loads(r["meta"] or "{}"),
    )
