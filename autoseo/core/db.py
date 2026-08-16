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

SCHEMA_VERSION = 9

SCHEMA = """
-- Phase 0: measurement -------------------------------------------------------

-- Two tables because GSC returns different totals at different granularities, and the difference
-- is enormous. Measured against a UI export for 2026-04-30..2026-07-29 (true total 7,828):
--
--   dims=[date]                 7,828   complete
--   dims=[date, page]          10,724   complete  <- what we collect for pages
--   dims=[date, page, device]   1,439   87% LOST  <- adding `device` destroys it
--   dims=[query]                1,098   query data has its own, much lower ceiling
--
-- So `device` is never requested: it adds nothing we need and silently drops most rows when
-- combined with `page`. Query-level data is collected separately and is understood to be a subset
-- (GSC withholds anonymised queries), which is why nothing page-level is derived from it.
--
-- Page impressions (10,724) legitimately exceed site impressions (7,828): a single search that
-- shows two of our pages counts once for the property and once per page.

CREATE TABLE IF NOT EXISTS gsc_page_daily (
    date        TEXT NOT NULL,
    page        TEXT NOT NULL,
    clicks      REAL NOT NULL DEFAULT 0,
    impressions REAL NOT NULL DEFAULT 0,
    ctr         REAL NOT NULL DEFAULT 0,
    position    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, page)
);
CREATE INDEX IF NOT EXISTS ix_gsc_page_daily_date ON gsc_page_daily(date);

CREATE TABLE IF NOT EXISTS gsc_query_daily (
    date        TEXT NOT NULL,
    query       TEXT NOT NULL,
    clicks      REAL NOT NULL DEFAULT 0,
    impressions REAL NOT NULL DEFAULT 0,
    ctr         REAL NOT NULL DEFAULT 0,
    position    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, query)
);
CREATE INDEX IF NOT EXISTS ix_gsc_query_daily_date ON gsc_query_daily(date);

-- The page<->query mapping. Lossy by nature (1,439 of 7,828) — used only to answer "which query is
-- this page closest on", never to measure volume.
CREATE TABLE IF NOT EXISTS gsc_page_query (
    date        TEXT NOT NULL,
    page        TEXT NOT NULL,
    query       TEXT NOT NULL,
    clicks      REAL NOT NULL DEFAULT 0,
    impressions REAL NOT NULL DEFAULT 0,
    position    REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, page, query)
);

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

-- One row per (question, engine, run). Repeats matter: answers vary run to run, so a single
-- result is not a measurement — everything downstream reads these as rates.
CREATE TABLE IF NOT EXISTS aeo_probe (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    question_id TEXT NOT NULL,
    question    TEXT NOT NULL,
    engine      TEXT NOT NULL,
    run         INTEGER NOT NULL DEFAULT 1,
    mentioned   INTEGER NOT NULL DEFAULT 0,
    cited       INTEGER NOT NULL DEFAULT 0,
    competitors TEXT,
    answer      TEXT
);
CREATE INDEX IF NOT EXISTS ix_aeo_probe_ts ON aeo_probe(ts);

-- Every source the engine cited. This is the outreach target list: pages answer engines already
-- trust for our buyer questions, which is what getting listed on them is worth.
CREATE TABLE IF NOT EXISTS aeo_citation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    question_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    domain      TEXT NOT NULL,
    title       TEXT
);
CREATE INDEX IF NOT EXISTS ix_aeo_citation_domain ON aeo_citation(domain);

-- Outreach targets, with state. The scoring in `decide/outreach.py` is a pure function over probe
-- data, which meant every run reprinted the same thirty rows with no idea which had been acted on —
-- a report, not a workload. Persisting them adds the two things a report cannot have: a memory of
-- what was already handled, and a before/after on `listed`, which is the only evidence that any of
-- this works. Nothing here sends anything; a human does the sending and this records the outcome.
CREATE TABLE IF NOT EXISTS outreach_target (
    url            TEXT PRIMARY KEY,
    domain         TEXT NOT NULL,
    title          TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    citations      INTEGER NOT NULL DEFAULT 0,
    score          REAL NOT NULL DEFAULT 0,
    competitors    TEXT,
    -- new | contacted | listed | declined | skipped. `listed` is set by measurement, not by hand:
    -- the page began naming DailyVox when it previously did not.
    state          TEXT NOT NULL DEFAULT 'new',
    state_changed  TEXT NOT NULL,
    listed_at      TEXT,
    note           TEXT
);
CREATE INDEX IF NOT EXISTS ix_outreach_state ON outreach_target(state);

-- The ledger: every action the loop decided to take, why, and what happened. It began as an
-- approval queue and kept its shape when the approvals went away, because the columns that
-- mattered were never about permission — `rationale` is required because an action that cannot
-- explain itself is a bug, and `decided_at` / `decided_by` are what make a bad week auditable
-- after the fact. `message_id` is vestigial: it held the Telegram message a card was sent as.
CREATE TABLE IF NOT EXISTS queue_item (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    rationale   TEXT,
    policy      TEXT NOT NULL DEFAULT 'gated',
    meta        TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    message_id  INTEGER,
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_status ON queue_item(status);
CREATE INDEX IF NOT EXISTS ix_queue_message ON queue_item(message_id);

-- Indexation, as a time series rather than a snapshot.
--
-- `url_index_status` holds one row per URL and is overwritten on every inspection, so it can say
-- what the ratio is today and nothing about whether it is moving. That is the wrong shape for the
-- only number that matters here: 46 of 140 blog pages are not indexed, and every decision about
-- whether to publish more or prune harder depends on which way that is going. One row per day per
-- cluster, appended, never overwritten.
CREATE TABLE IF NOT EXISTS index_health (
    date     TEXT NOT NULL,
    cluster  TEXT NOT NULL,
    in_sitemap INTEGER NOT NULL DEFAULT 0,
    checked  INTEGER NOT NULL DEFAULT 0,
    indexed  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, cluster)
);

-- Shingle index of the existing site, for duplication checking. Hashes only: the index stays small
-- enough to commit and reveals nothing about page content. Built locally from public/, used in CI.
CREATE TABLE IF NOT EXISTS corpus_shingle (
    url    TEXT PRIMARY KEY,
    n      INTEGER NOT NULL,
    hashes TEXT NOT NULL
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


def _repair_citation_domains(conn: sqlite3.Connection) -> int:
    """Backfill aeo_citation.domain for rows stored before the extraction bug was fixed.

    Gemini returns grounding URIs as vertexaisearch.cloud.google.com redirects and puts the real
    source domain in the chunk title. Early rows therefore recorded Google's host for every citation,
    which made every outreach target look like a skip-list match and the module returned nothing.

    Written as a migration rather than a one-off script because the first repair was run locally,
    never reached the committed snapshot, and the bug silently came back.
    """
    rows = conn.execute(
        "SELECT id, title FROM aeo_citation WHERE domain LIKE '%vertexaisearch%'"
    ).fetchall()
    fixed = 0
    for r in rows:
        title = (r["title"] or "").strip().lower()
        if title and " " not in title and "." in title and "/" not in title:
            conn.execute("UPDATE aeo_citation SET domain = ? WHERE id = ?",
                         (title.removeprefix("www."), r["id"]))
            fixed += 1
    return fixed


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _repair_citation_domains(conn)
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
