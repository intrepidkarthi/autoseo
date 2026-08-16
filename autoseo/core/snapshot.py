"""Persist state as sorted CSV, not as a SQLite binary.

Committing the .db was the wrong call and it cost real data. Git cannot merge a binary file, so in
the space of two days it produced a rejected push, two unresolvable conflicts, a commit that had to
be recovered from the reflog, and finally a silent wipe of the 16-month backfill when a conflict was
resolved by taking the wrong side.

CSV fixes all of it: git merges it line by line, a pull request shows exactly which rows changed, and
a bad merge is visible rather than silent. The database becomes what it should have been from the
start — a local query cache rebuilt from the snapshot on demand.

Rows are written in a deterministic order so an unchanged table produces a zero-line diff. Without
that, every run would rewrite every file and the history would be useless.
"""

from __future__ import annotations

import csv
from pathlib import Path

from autoseo.core.config import settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

# Order matters only for readability of the directory listing.
TABLES: tuple[str, ...] = (
    "gsc_page_daily",
    "gsc_query_daily",
    "gsc_page_query",
    "url_inventory",
    "url_index_status",
    "index_health",
    "bing_daily",
    "aeo_probe",
    "aeo_citation",
    "spend_ledger",
    "run_log",
    "queue_item",
    "outreach_target",
)
# corpus_shingle is deliberately absent. Committing it cost 5.7 MB per refresh — a full rewrite of
# the file every time any page changes, which is most weeks. It is a cache, not state: `plan`
# rebuilds it from the live site in about a minute, so it is always fresher than a committed copy
# would have been anyway.

# Sort keys chosen to be stable and to group related rows together in the diff.
SORT_KEYS: dict[str, str] = {
    "gsc_page_daily": "date, page",
    "gsc_query_daily": "date, query",
    "gsc_page_query": "date, page, query",
    "url_inventory": "url",
    "url_index_status": "url",
    "index_health": "date, cluster",
    "bing_daily": "date, metric",
    "aeo_probe": "ts, question_id, run",
    "aeo_citation": "ts, question_id, url",
    "spend_ledger": "ts, id",
    "run_log": "started, id",
    "queue_item": "id",
    "outreach_target": "url",
    "corpus_shingle": "url",
}


def _path(table: str) -> Path:
    return settings.state_dir / f"{table}.csv"


def dump() -> dict[str, int]:
    """Write every table to state/<table>.csv. Returns row counts."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with session() as conn:
        for table in TABLES:
            try:
                rows = conn.execute(
                    f"SELECT * FROM {table} ORDER BY {SORT_KEYS.get(table, '1')}"
                ).fetchall()
            except Exception as exc:  # noqa: BLE001 — a table may not exist on an older schema
                log.warning("skip %s: %s", table, exc)
                continue
            cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]
            with _path(table).open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(cols)
                for r in rows:
                    w.writerow(["" if r[c] is None else r[c] for c in cols])
            counts[table] = len(rows)
    log.info("snapshot: %s", ", ".join(f"{t}={n}" for t, n in counts.items() if n))
    return counts


def load() -> dict[str, int]:
    """Rebuild the database from the CSV snapshot. Safe to run on a fresh checkout."""
    counts: dict[str, int] = {}
    with session() as conn:
        for table in TABLES:
            path = _path(table)
            if not path.exists():
                continue
            with path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)
                if not rows:
                    continue
                cols = reader.fieldnames or []
                placeholders = ", ".join("?" * len(cols))
                # INSERT OR REPLACE so a re-load is idempotent and never duplicates.
                conn.executemany(
                    f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES ({placeholders})",
                    [[(r[c] if r[c] != "" else None) for c in cols] for r in rows],
                )
                counts[table] = len(rows)
    if counts:
        log.info("restored: %s", ", ".join(f"{t}={n}" for t, n in counts.items()))
    return counts
