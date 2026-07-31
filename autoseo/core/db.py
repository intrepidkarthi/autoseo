"""SQLite datastore. The file lives in state/ and is committed to the repo.

Committing state buys three things at once: free persistence with no database to host,
a full audit log in git history, and it keeps GitHub's 60-day scheduled-workflow timer alive
(schedules are disabled after 60 days without a commit).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from autoseo.core.config import settings

SCHEMA_VERSION = 2

SCHEMA = """
-- Phase 0: measurement -------------------------------------------------------

CREATE TABLE IF NOT EXISTS gsc_daily (
    date        TEXT NOT NULL,
    query       TEXT NOT NULL,
    page        TEXT NOT NULL,
    device      TEXT NOT NULL DEFAULT '',
    clicks      REAL NOT NULL DEFAULT 0,
    impressions REAL NOT NULL DEFAULT 0,
    ctr         REAL NOT NULL DEFAULT 0,
    position    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, query, page, device)
);
CREATE INDEX IF NOT EXISTS ix_gsc_daily_date ON gsc_daily(date);
CREATE INDEX IF NOT EXISTS ix_gsc_daily_page ON gsc_daily(page);

-- Page-level totals collected WITHOUT the query dimension.
-- This is not a convenience rollup of gsc_daily: asking GSC for the `query` dimension makes it drop
-- anonymised (rare) queries entirely, so summing gsc_daily by page undercounts badly — measured at
-- 87% missing on getdailyvox.com. Anything reasoning about page performance must use this table.
CREATE TABLE IF NOT EXISTS gsc_page_daily (
    date        TEXT NOT NULL,
    page        TEXT NOT NULL,
    device      TEXT NOT NULL DEFAULT '',
    clicks      REAL NOT NULL DEFAULT 0,
    impressions REAL NOT NULL DEFAULT 0,
    ctr         REAL NOT NULL DEFAULT 0,
    position    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, page, device)
);
CREATE INDEX IF NOT EXISTS ix_gsc_page_daily_date ON gsc_page_daily(date);

-- One row per URL we know about, whether or not it is in the sitemap.
-- cluster is derived from the path: core | blog | for | in | alternative | use | other
CREATE TABLE IF NOT EXISTS url_inventory (
    url        TEXT PRIMARY KEY,
    cluster    TEXT NOT NULL,
    in_sitemap INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_url_inventory_cluster ON url_inventory(cluster);

CREATE TABLE IF NOT EXISTS url_index_status (
    url             TEXT PRIMARY KEY,
    checked_at      TEXT NOT NULL,
    verdict         TEXT,   -- PASS | PARTIAL | FAIL | NEUTRAL
    coverage_state  TEXT,   -- e.g. "Submitted and indexed", "Crawled - currently not indexed"
    indexed         INTEGER NOT NULL DEFAULT 0,
    robots_state    TEXT,
    canonical_user  TEXT,
    canonical_google TEXT,
    last_crawl_time TEXT,
    rich_results    TEXT,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS ix_url_index_checked ON url_index_status(checked_at);

CREATE TABLE IF NOT EXISTS bing_daily (
    date        TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, metric)
);

CREATE TABLE IF NOT EXISTS spend_ledger (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    provider  TEXT NOT NULL,
    operation TEXT NOT NULL,
    units     REAL NOT NULL DEFAULT 0,
    usd       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_spend_ts ON spend_ledger(ts);

CREATE TABLE IF NOT EXISTS run_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started  TEXT NOT NULL,
    command  TEXT NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _cluster_for(url: str) -> str:
    """Bucket a URL by its first path segment. This is what makes the indexation
    report actionable — the whole Phase 1 decision is per-cluster."""
    path = url.split("//", 1)[-1].split("/", 1)
    rest = path[1] if len(path) > 1 else ""
    head = rest.split("/", 1)[0].lower()
    if not head or head.endswith(".xml") or head.endswith(".txt"):
        return "core"
    if head in {"blog", "for", "in", "alternative", "use", "reports", "research", "press"}:
        return head
    return "core" if "." not in head else "other"


def cluster_for(url: str) -> str:
    return _cluster_for(url)


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Deliberately NOT WAL. This database is committed to git, and WAL leaves committed rows in a
    # separate -wal file — so a commit could capture a .db that is missing the run's data.
    # DELETE journalling keeps everything in the single file. Performance is irrelevant at this size.
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
