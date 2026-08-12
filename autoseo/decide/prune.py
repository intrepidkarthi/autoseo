"""What to stop publishing, and what to stop submitting.

The site's whole problem is on this side of the ledger. 1,507 templated pages were pulled from the
sitemap for scaled-content risk, and the same shape grew back inside the blog one level down:

    /blog/journal-prompts-*     10 pages      0 impressions / 90d
    /blog/voice-journal-for-*    7 pages      6 impressions / 90d
    /blog/dailyvox-vs-*         25 pages    624 impressions / 90d   <- this one works

Thirty pages producing twenty-five impressions between them, sitting in the sitemap, spending crawl
budget that 46 unindexed blog pages need. Google's answer is already recorded in the index status:
"Discovered — currently not indexed" on thirty URLs means it knows they exist and has declined to
fetch them.

So the detection is deliberately arithmetic, not judgement. A slug prefix is dead when it has enough
pages to read as a template, produces almost nothing per page, and has never earned a click. Every
threshold below is a guard against pruning something that works — `dailyvox-vs-*` clears all three
and is exactly what must survive.

Nothing here touches the site. It reads the database and returns a decision with its evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from autoseo.core.db import session

# A prefix needs this many pages before it reads as a template rather than a few related posts.
MIN_PAGES = 5
# Impressions per page over the window, below which the page is not being shown to anyone at all.
#
# Set at 1.0 rather than 3.0 after measuring what the looser threshold caught. `/blog/how-to-*` sits
# at 1.5 per page, and those thirteen pages are topically unrelated — exporting Day One, journalling
# with ADHD, starting a voice journal — that happen to share a naming convention. A shared prefix is
# not a template. They deserve improving, not switching off, and 1.0 draws the line where a page has
# failed to produce a single impression in ninety days.
MAX_IMPRESSIONS_PER_PAGE = 1.0
# Any click at all is proof the cluster reaches someone. Keep it. This is what spares
# `/blog/voice-journal-for-*`: seven pages, six impressions, and one person who found what they
# were looking for.
MAX_CLICKS = 0

# Never prune these, whatever the arithmetic says. Release notes are dated by nature and will always
# look thin; the comparison cluster is the blog's best performer and a threshold change must never
# be able to take it out.
#
# No trailing hyphens: `_prefix_of` returns `dailyvox-vs`, not `dailyvox-vs-`. The first version of
# this tuple had them, so `startswith` never matched and the guard on the blog's best-performing
# cluster silently did nothing — it survived on the impressions threshold alone.
PROTECTED = ("dailyvox-vs", "body-twin", "speak-your-first")


# Duplication is deliberately not part of the test, having been measured and found irrelevant here.
# Median pairwise shingle similarity inside these clusters is 4-6% — `journal-prompts-for-anger` and
# `journal-prompts-for-relationships` are genuinely different text. Google is not declining to index
# them for near-duplication; it is declining because nobody searches for them and nothing links to
# them. The demand numbers already say that, and adding a similarity term would only make a working
# rule harder to read.


@dataclass
class DeadCluster:
    prefix: str
    pages: int
    indexed: int
    impressions: float
    clicks: float
    urls: list[str] = field(default_factory=list)

    @property
    def evidence(self) -> str:
        return (f"{self.pages} pages, {self.impressions:.0f} impressions and {self.clicks:.0f} "
                f"clicks in 90 days ({self.impressions / self.pages:.1f} impressions per page). "
                f"{self.indexed} of {self.pages} are indexed.")


def _prefix_of(url: str) -> str:
    """The slug prefix a page belongs to: /blog/journal-prompts-for-anger -> journal-prompts-.

    Two segments, because one is too coarse (every blog page starts with the same letter runs) and
    three splits genuine clusters apart. `voice-journal-for-runners` and `voice-journal-for-anxiety`
    have to land together or the count never reaches the threshold.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    parts = slug.split("-")
    if len(parts) < 3:
        return ""
    return "-".join(parts[:3]) if parts[2] in ("for", "vs", "to", "of") else "-".join(parts[:2])


def dead_clusters(days: int = 90) -> list[DeadCluster]:
    """Slug prefixes inside /blog/ that are not earning their place in the index."""
    with session() as conn:
        rows = conn.execute(
            """
            -- `indexed` is a SQLite keyword (INDEXED BY), so the alias has to be something else.
            SELECT u.url, COALESCE(i.indexed, 0) is_indexed,
                   COALESCE((SELECT SUM(g.impressions) FROM gsc_page_daily g
                             WHERE g.page = u.url AND g.date >= date('now', ?)), 0) imp,
                   COALESCE((SELECT SUM(g.clicks) FROM gsc_page_daily g
                             WHERE g.page = u.url AND g.date >= date('now', ?)), 0) clk
            FROM url_inventory u LEFT JOIN url_index_status i ON i.url = u.url
            WHERE u.cluster = 'blog' AND u.in_sitemap = 1
            """,
            (f"-{days} days", f"-{days} days"),
        ).fetchall()

    buckets: dict[str, DeadCluster] = {}
    for r in rows:
        prefix = _prefix_of(r["url"])
        if not prefix or prefix.startswith(PROTECTED):
            continue
        c = buckets.setdefault(prefix, DeadCluster(prefix=f"{prefix}-", pages=0, indexed=0,
                                                   impressions=0.0, clicks=0.0))
        c.pages += 1
        c.indexed += int(r["is_indexed"])
        c.impressions += float(r["imp"])
        c.clicks += float(r["clk"])
        c.urls.append(r["url"])

    dead = [
        c for c in buckets.values()
        if c.pages >= MIN_PAGES
        and c.clicks <= MAX_CLICKS
        and c.impressions / c.pages <= MAX_IMPRESSIONS_PER_PAGE
    ]
    dead.sort(key=lambda c: -c.pages)
    return dead


# --- sitemap hygiene ---------------------------------------------------------------------------

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
# Paginated listings. They are navigation, not content: submitting them asks Google to index the
# same articles a second time under a URL nobody links to and nobody searches for.
_PAGINATION = re.compile(r"/page/\d+/?$")


def sitemap_urls(xml: str) -> list[str]:
    return _LOC.findall(xml)


def sitemap_problems(xml: str, dead_urls: set[str]) -> dict[str, list[str]]:
    """Entries that should not be in a sitemap: pagination, and URLs that do not resolve.

    A sitemap is a set of assertions about pages worth indexing. A 404 in it is a wrong assertion,
    and Google reads a wrong assertion as a reason to trust the rest of the file less.
    """
    problems: dict[str, list[str]] = {"pagination": [], "dead": []}
    for url in sitemap_urls(xml):
        if _PAGINATION.search(url):
            problems["pagination"].append(url)
        elif url in dead_urls:
            problems["dead"].append(url)
    return {k: v for k, v in problems.items() if v}
