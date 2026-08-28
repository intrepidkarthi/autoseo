"""`in_sitemap` is the flag `health.record` groups on — the number the strategy is steered by.

`build` only ever upserted URLs it currently found, so a URL dropped from the sitemap fell out of
the loop's reach and kept `in_sitemap = 1` for good. Nineteen rows were wrong by 2026-08-26,
including the ten `journal-prompts-*` pages this loop pruned itself.
"""
from __future__ import annotations

import pytest

from autoseo.collect import inventory
from autoseo.core.db import session

SITE = "https://getdailyvox.com"
LISTED = {f"{SITE}/", f"{SITE}/blog/kept-one", f"{SITE}/blog/kept-two"}
DELISTED = f"{SITE}/blog/journal-prompts-for-anger"


@pytest.fixture
def seeded(db, monkeypatch):
    """An inventory that still believes a de-listed page is in the sitemap."""
    monkeypatch.setattr(inventory, "_fetch_sitemap_urls", lambda root: (set(LISTED), {root}))
    with session() as conn:
        for url in LISTED | {DELISTED}:
            conn.execute(
                "INSERT INTO url_inventory(url, cluster, in_sitemap, first_seen) "
                "VALUES (?,?,1,'2026-08-01')", (url, "blog"))
    return db


def _flagged() -> set[str]:
    with session() as conn:
        return {r["url"] for r in conn.execute(
            "SELECT url FROM url_inventory WHERE in_sitemap = 1")}


def test_a_delisted_url_loses_the_flag(seeded):
    assert DELISTED in _flagged()
    inventory.build(None)
    assert DELISTED not in _flagged()


def test_listed_urls_keep_the_flag(seeded):
    inventory.build(None)
    assert _flagged() == LISTED


def test_the_row_survives_so_its_history_is_not_lost(seeded):
    """De-listed is not deleted. The URL stays in the inventory with the flag cleared."""
    inventory.build(None)
    with session() as conn:
        row = conn.execute("SELECT * FROM url_inventory WHERE url = ?", (DELISTED,)).fetchone()
    assert row is not None and row["in_sitemap"] == 0


def test_a_failed_sitemap_fetch_does_not_wipe_every_flag(seeded, monkeypatch):
    """The dangerous case. A sitemap that fails to load would otherwise clear the whole site and
    read the next morning as a total de-listing — so the clear is guarded on a non-empty fetch."""
    monkeypatch.setattr(inventory, "_fetch_sitemap_urls", lambda root: (set(), {root}))
    inventory.build(None)
    assert _flagged() == LISTED | {DELISTED}, "an empty fetch cleared flags it should not have"
