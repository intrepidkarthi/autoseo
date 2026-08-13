"""What the system decided to do, why, and what happened — the record that replaced the approval.

The old queue existed to ask permission. This one exists to leave evidence. Same table, different
job: every item still has to carry a `rationale`, because an action that cannot explain itself is a
bug whether or not anyone was going to read it.

Statuses are new values in the old column (`planned` / `shipped` / `failed` / `dropped`) rather than
the old `pending` / `approved` / `posted`. That is deliberate — the 36 items left pending when the
Telegram gate was switched off must never be picked up and shipped by the autonomous loop months
after they were composed against numbers that have since moved.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from enum import StrEnum

from autoseo.core.db import session


class Status(StrEnum):
    PLANNED = "planned"   # composed, cleared the quality gate, waiting for the apply step
    SHIPPED = "shipped"   # committed to the site repo
    FAILED = "failed"     # apply tried and could not
    DROPPED = "dropped"   # superseded or no longer valid — never attempted


class Kind(StrEnum):
    POST = "post"         # a new article
    META = "meta"         # title + meta description rewrite on a live article
    FAQ = "faq"           # an FAQ section appended to a live article
    RELINK = "relink"     # link orphaned live pages from the blog index
    DELIST = "delist"     # noindex headers for the dead clusters
    PRUNE = "prune"       # noindex a blog cluster that earns nothing
    SITEMAP = "sitemap"   # drop URLs that should never have been submitted
    MERGE = "merge"       # 301 a duplicate page onto the one that outranks it


@dataclass
class Item:
    kind: str
    title: str
    body: str
    rationale: str
    channel: str = "blog"
    policy: str = "auto"
    meta: dict = field(default_factory=dict)
    id: int | None = None
    status: str = Status.PLANNED
    created: str = ""
    decided_at: str | None = None
    decided_by: str | None = None


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def plan(item: Item) -> int:
    with session() as conn:
        cur = conn.execute(
            """INSERT INTO queue_item(created, kind, channel, title, body, rationale,
                                      policy, meta, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_now(), item.kind, item.channel, item.title, item.body, item.rationale,
             item.policy, json.dumps(item.meta), Status.PLANNED),
        )
        return int(cur.lastrowid)


def planned(kind: str | None = None) -> list[Item]:
    sql = "SELECT * FROM queue_item WHERE status = ?"
    args: list = [Status.PLANNED]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    with session() as conn:
        return [_row(r) for r in conn.execute(sql + " ORDER BY id", args)]


def get(item_id: int) -> Item | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM queue_item WHERE id = ?", (item_id,)).fetchone()
    return _row(row) if row else None


def _settle(item_id: int, status: Status, by: str, **meta_updates) -> None:
    with session() as conn:
        row = conn.execute("SELECT meta FROM queue_item WHERE id = ?", (item_id,)).fetchone()
        meta = json.loads((row["meta"] if row else "") or "{}")
        meta.update(meta_updates)
        conn.execute(
            "UPDATE queue_item SET status = ?, decided_at = ?, decided_by = ?, meta = ? "
            "WHERE id = ?",
            (status, _now(), by, json.dumps(meta), item_id),
        )


def ship(item_id: int, commit_url: str) -> None:
    _settle(item_id, Status.SHIPPED, "apply", commit=commit_url)


def fail(item_id: int, reason: str) -> None:
    _settle(item_id, Status.FAILED, "apply", error=reason[:500])


def drop(item_id: int, reason: str) -> None:
    _settle(item_id, Status.DROPPED, "plan", reason=reason[:500])


def shipped_today(kind: str) -> int:
    """How many items of this kind shipped on today's UTC date.

    A calendar day, not a rolling 24 hours. Under a rolling window a run that drifts late pushes the
    next one out of its slot and the day is silently skipped — which is what a manual run at 17:15
    did to the following 00:37 cron. "One a day" should mean what a person means by it.
    """
    today = dt.datetime.now(dt.UTC).date().isoformat()
    with session() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM queue_item WHERE kind = ? AND status = ? "
            "AND substr(decided_at, 1, 10) = ?",
            (kind, Status.SHIPPED, today),
        ).fetchone()[0]


def shipped_since(kind: str, days: int) -> int:
    """How many items of this kind actually reached the site in the last N days."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with session() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM queue_item WHERE kind = ? AND status = ? AND decided_at >= ?",
            (kind, Status.SHIPPED, cutoff),
        ).fetchone()[0]


def summary(days: int = 30) -> list[tuple[str, str, int]]:
    """(kind, status, count) over the recent window, for `autoseo status`."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    with session() as conn:
        return [
            (r["kind"], r["status"], r["n"])
            for r in conn.execute(
                "SELECT kind, status, COUNT(*) n FROM queue_item WHERE created >= ? "
                "GROUP BY kind, status ORDER BY kind, status",
                (cutoff,),
            )
        ]


def recent(limit: int = 12) -> list[Item]:
    with session() as conn:
        return [
            _row(r) for r in conn.execute(
                "SELECT * FROM queue_item WHERE status IN (?,?,?) ORDER BY id DESC LIMIT ?",
                (Status.SHIPPED, Status.FAILED, Status.PLANNED, limit),
            )
        ]


def recent_sources(kind: str, limit: int = 2) -> list[str]:
    """Which channel drove the last few items of this kind — newest first.

    Used to alternate between Google-measured demand and answer-engine gaps. Without it the loop
    would work whichever channel ranks highest today, forever, and the other one would never get a
    post written for it.
    """
    with session() as conn:
        rows = conn.execute(
            "SELECT meta FROM queue_item WHERE kind = ? AND status = ? ORDER BY id DESC LIMIT ?",
            (kind, Status.SHIPPED, limit),
        ).fetchall()
    return [json.loads(r["meta"] or "{}").get("source", "gsc") for r in rows]


def slugs_touched(kinds: tuple[str, ...], days: int) -> set[str]:
    """Slugs already acted on recently — so the fixer does not rewrite the same page every day."""
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat(timespec="seconds")
    out: set[str] = set()
    with session() as conn:
        for r in conn.execute(
            f"SELECT meta FROM queue_item WHERE kind IN ({','.join('?' * len(kinds))}) "
            "AND created >= ?",
            (*kinds, cutoff),
        ):
            slug = json.loads(r["meta"] or "{}").get("slug")
            if slug:
                out.add(slug)
    return out


def _row(r) -> Item:
    return Item(
        id=r["id"], kind=r["kind"], channel=r["channel"], title=r["title"], body=r["body"],
        rationale=r["rationale"], policy=r["policy"], status=r["status"],
        created=r["created"], decided_at=r["decided_at"], decided_by=r["decided_by"],
        meta=json.loads(r["meta"] or "{}"),
    )
