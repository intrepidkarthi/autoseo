"""Duplication checks — internal first, because internal is what actually bites.

External plagiarism is the famous risk. For this site it is the smaller one. getdailyvox.com already
carries 1,724 pages, 1,568 of them templated and de-listed precisely because near-duplicate content
at scale is what Google's spam policy targets. Publishing another page that closely echoes an
existing one feeds exactly the problem the de-listing was meant to fix.

So the primary check is against our own corpus, using shingling: overlapping word 5-grams hashed
into a set, compared by Jaccard similarity. It is cheap, deterministic, needs no network, and
catches the real failure mode — a new post that restates a page we already have.

The corpus index is built once from the site's `public/` directory and committed as hashes only, so
CI can check without needing the site checked out or fetching 1,724 pages.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

SHINGLE_SIZE = 5
# Above this, the draft is substantially a restatement of a page we already publish.
BLOCK_THRESHOLD = 0.28
WARN_THRESHOLD = 0.18


@dataclass
class DuplicationHit:
    url: str
    similarity: float

    @property
    def blocking(self) -> bool:
        return self.similarity >= BLOCK_THRESHOLD


def _normalise(text: str) -> list[str]:
    text = re.sub(r"<[^>]+>", " ", text)          # strip html
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return text.lower().split()


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[int]:
    """Hashed overlapping n-grams. Storing 8-byte hashes rather than the text keeps the committed
    index small and means the index leaks nothing about page content."""
    words = _normalise(text)
    if len(words) < size:
        return set()
    return {
        int.from_bytes(hashlib.blake2b(" ".join(words[i:i + size]).encode(), digest_size=8).digest(), "big")
        for i in range(len(words) - size + 1)
    }


def jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_index(public_dir: Path) -> int:
    """Index the site's existing pages. Run locally; the result is committed for CI to use."""
    # Keyed by URL, not path: Vercel's cleanUrls means /blog/foo.html and /blog/foo/index.html
    # resolve to the same page, so a dict collapses the collision instead of failing on it.
    rows: dict[str, tuple[str, int, str]] = {}
    for path in sorted(public_dir.rglob("*.html")):
        rel = path.relative_to(public_dir).as_posix()
        url = "/" + (rel.removesuffix("/index.html") if rel.endswith("/index.html")
                     else rel.removesuffix(".html"))
        sh = shingles(path.read_text(encoding="utf-8", errors="ignore"))
        if len(sh) < 50:      # skip stubs and redirect shells
            continue
        rows[url] = (url, len(sh), ",".join(str(h) for h in sorted(sh)))

    with session() as conn:
        conn.execute("DELETE FROM corpus_shingle")
        conn.executemany(
            "INSERT INTO corpus_shingle(url, n, hashes) VALUES (?,?,?)", list(rows.values())
        )
    log.info("indexed %d pages for duplication checking", len(rows))
    return len(rows)


CORPUS_STAMP = "corpus_indexed_at"
CORPUS_MAX_AGE_DAYS = 7


def indexed_at() -> str:
    with session() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (CORPUS_STAMP,)).fetchone()
    return row["value"] if row else ""


def refresh_from_live(clusters: tuple[str, ...] = ("blog", "core"),
                      max_age_days: int = CORPUS_MAX_AGE_DAYS, limit: int = 200) -> int:
    """Rebuild the index by fetching our own live pages. Returns pages indexed, 0 if still fresh.

    The committed index was empty, which meant the duplication check quietly passed everything: an
    unattended writer with a disabled duplication check is precisely how a site grows another 1,500
    near-identical pages. Building it from the live sitemap rather than a local `public/` directory
    is what makes it work in CI, where the site repo is not checked out and no credential for it
    exists in the composing environment.

    Only the clusters a new post could plausibly restate are indexed. The templated `/for/` and
    `/in/` pages are being de-listed and are not worth the bytes in git.
    """
    import datetime as dt

    stamp = indexed_at()
    if stamp and corpus_size():
        age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(stamp)
        if age.days < max_age_days:
            log.info("corpus is %d day(s) old — not refreshing", age.days)
            return 0

    import httpx

    with session() as conn:
        urls = [
            r["url"] for r in conn.execute(
                f"SELECT url FROM url_inventory WHERE in_sitemap = 1 AND cluster IN "
                f"({','.join('?' * len(clusters))}) ORDER BY url LIMIT ?",
                (*clusters, limit),
            )
        ]

    rows: list[tuple[str, int, str]] = []
    for url in urls:
        try:
            r = httpx.get(url, timeout=30.0, follow_redirects=True,
                          headers={"user-agent": "autoseo"})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — one unreachable page must not lose the rest
            log.warning("corpus: %s unreachable (%s)", url, str(exc)[:60])
            continue
        sh = shingles(r.text)
        if len(sh) < 50:
            continue
        path = "/" + url.split("//", 1)[-1].split("/", 1)[-1] if "//" in url else url
        rows.append((path.rstrip("/") or "/", len(sh), ",".join(str(h) for h in sorted(sh))))

    if not rows:
        log.warning("corpus refresh fetched nothing — leaving the existing index in place")
        return 0

    with session() as conn:
        conn.execute("DELETE FROM corpus_shingle")
        conn.executemany("INSERT INTO corpus_shingle(url, n, hashes) VALUES (?,?,?)", rows)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value",
            (CORPUS_STAMP, dt.datetime.now(dt.UTC).isoformat(timespec="seconds")),
        )
    log.info("corpus: indexed %d live page(s)", len(rows))
    return len(rows)


def check(text: str, top: int = 3) -> list[DuplicationHit]:
    """Compare a draft against every indexed page. Returns the closest matches, worst first."""
    draft = shingles(text)
    if not draft:
        return []

    hits: list[DuplicationHit] = []
    with session() as conn:
        for row in conn.execute("SELECT url, hashes FROM corpus_shingle"):
            existing = {int(h) for h in row["hashes"].split(",") if h}
            sim = jaccard(draft, existing)
            if sim >= WARN_THRESHOLD:
                hits.append(DuplicationHit(row["url"], sim))

    hits.sort(key=lambda h: -h.similarity)
    return hits[:top]


def corpus_size() -> int:
    with session() as conn:
        return conn.execute("SELECT COUNT(*) FROM corpus_shingle").fetchone()[0]
