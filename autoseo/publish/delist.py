"""Finish the de-listing that removing pages from the sitemap only started.

Those 1,568 pages were pulled from the sitemap deliberately, to avoid a scaled-content penalty. That
instinct was right, but sitemap removal only withdraws a *discovery hint*. It does not deindex, does
not stop crawling, and does not remove the pages from site-wide quality signals. They all still
return 200 and robots.txt still allows every crawler, so the protection intended was never actually
in place.

What the data says, over 90 days:

    /for/          1,108 pages   162 impressions   1 click
    /in/             399 pages    88 impressions   0 clicks
    /use/             40 pages    92 impressions   1 click
    /alternative/     20 pages   163 impressions   0 clicks

/for/ and /in/ are 1,507 pages returning 250 impressions and one click between them. City pages for
an iPhone journaling app are the textbook shape the scaled-content policy targets. They go.

/use/ and /alternative/ are 60 pages carrying half the orphan traffic and genuine intent — someone
searching "Day One alternative" is choosing between products. They stay, and get listed properly.

`noindex` rather than 410, deliberately. It is reversible, it needs no file deletion, and if this
turns out to be wrong the fix is deleting a config block rather than restoring 1,507 files from git.
Google must crawl a page to see the header, which is why robots.txt must keep allowing them — the
common mistake here is to Disallow them as well, which *preserves* the index entry by making the
noindex unreachable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from autoseo.core.db import session

# Clusters to remove from the index, with the evidence for each.
REMOVE = {
    "for": "1,108 pages, 162 impressions and 1 click in 90 days",
    "in": "399 city pages for an iPhone-only app, 88 impressions and 0 clicks in 90 days",
}

# Clusters worth keeping and listing properly.
KEEP = {
    "alternative": "20 pages, 163 impressions — comparison intent, the queries that convert",
    "use": "40 pages, 92 impressions — use-case intent, small enough not to read as scaled",
}


@dataclass
class Plan:
    noindex_globs: list[str]
    keep_globs: list[str]
    evidence: dict[str, str]
    survivors: list[tuple[str, float]]


def build_plan(min_impressions: float = 5) -> Plan:
    """Which clusters go, and which individual pages inside them earned a reprieve."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT page, SUM(impressions) im
            FROM gsc_page_daily
            WHERE date >= date('now', '-90 days')
              AND (page LIKE '%/for/%' OR page LIKE '%/in/%')
            GROUP BY page HAVING im >= ?
            ORDER BY im DESC
            """,
            (min_impressions,),
        ).fetchall()

    survivors = [(r["page"].replace("https://getdailyvox.com", ""), r["im"]) for r in rows]
    return Plan(
        noindex_globs=[f"/{c}/(.*)" for c in REMOVE],
        keep_globs=[f"/{c}/(.*)" for c in KEEP],
        evidence={**REMOVE, **KEEP},
        survivors=survivors,
    )


def vercel_headers(plan: Plan) -> list[dict]:
    """The headers block to merge into the site's vercel.json.

    X-Robots-Tag is the header form of a noindex meta tag and Google treats them identically. Using
    the header rather than editing 1,507 HTML files means the change is one config block, and
    undoing it is deleting that block.
    """
    return [
        {
            "source": f"/{cluster}/:path*",
            "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}],
        }
        for cluster in REMOVE
    ]


def noindex_urls(paths: list[str]) -> list[dict]:
    """Header entries for individual pages, by exact path.

    Exact paths rather than a pattern, deliberately. A regex covering `/blog/journal-prompts-for-.*`
    is three characters away from covering `/blog/.*`, and the failure mode of that typo is the
    whole blog dropping out of the index with nobody watching. Ten explicit entries are verbose and
    cannot over-match.
    """
    return [
        {"source": p, "headers": [{"key": "X-Robots-Tag", "value": "noindex, nofollow"}]}
        for p in sorted(paths)
    ]


def apply_noindex(paths: list[str], rationale: str, dry_run: bool = False) -> str:
    """Merge per-page noindex headers into vercel.json. Idempotent."""
    from autoseo.publish import site

    path = f"{site.site_dir()}/vercel.json"
    raw = site.read_text(path)
    if raw is None:
        raise RuntimeError(f"{path} not found in {site.SITE_REPO}")

    config = json.loads(raw)
    headers = config.setdefault("headers", [])
    existing = {h.get("source") for h in headers if isinstance(h, dict)}
    added = [h for h in noindex_urls(paths) if h["source"] not in existing]
    if not added:
        return ""

    headers.extend(added)
    listed = "\n".join(f"  {h['source']}" for h in added[:20])
    return site.commit(
        {path: json.dumps(config, indent=2) + "\n"},
        f"seo: noindex {len(added)} page(s)\n\n{rationale}\n\n{listed}",
        dry_run=dry_run,
    )


def applied(vercel_json: str) -> bool:
    """Is the noindex block already on the site?"""
    import json as _json
    try:
        headers = _json.loads(vercel_json).get("headers") or []
    except ValueError:
        return False
    sources = {h.get("source") for h in headers}
    return all(h["source"] in sources for h in vercel_headers(build_plan()))


def apply(dry_run: bool = False) -> str:
    """Merge the noindex headers into the site's vercel.json. One commit, idempotent.

    This is the highest-ROI action available to the whole system and it sat as printed text for
    weeks because it needed someone to paste it. 1,507 pages have been returning 200 with every
    crawler allowed, contributing to site-wide quality signals, since the day they were pulled from
    the sitemap — sitemap removal withdraws a discovery hint and nothing more.
    """
    from autoseo.publish import site

    path = f"{site.site_dir()}/vercel.json"
    raw = site.read_text(path)
    if raw is None:
        raise RuntimeError(f"{path} not found in {site.SITE_REPO} — cannot apply the noindex")

    config = json.loads(raw)
    headers = config.setdefault("headers", [])
    existing = {h.get("source") for h in headers if isinstance(h, dict)}
    plan = build_plan()
    added = [h for h in vercel_headers(plan) if h["source"] not in existing]
    if not added:
        print("  noindex headers are already on the site — nothing to do.")
        return ""

    headers.extend(added)
    body = json.dumps(config, indent=2) + "\n"

    why = "\n".join(f"  /{c}/  {REMOVE[c]}" for c in REMOVE)
    survivors = ""
    if plan.survivors:
        survivors = (f"\n\n{len(plan.survivors)} page(s) inside these clusters still earn "
                     f"impressions; they are switched off with the rest and are worth rewriting "
                     f"into real pages if that traffic matters.")
    return site.commit(
        {path: body},
        f"seo: noindex the de-listed clusters\n\n"
        f"X-Robots-Tag: noindex, nofollow on:\n{why}\n\n"
        f"Removing them from the sitemap only withdrew a discovery hint — the pages still return "
        f"200 and are still crawled. robots.txt deliberately keeps allowing them: Google has to "
        f"crawl a page to see the header.{survivors}",
        dry_run=dry_run,
    )


def render_patch(plan: Plan) -> str:
    """A human-readable summary plus the exact JSON to paste."""
    lines = [
        # Named without a path on purpose. The website moved out of solyn/ to the repo root once
        # already; a human reading this can find the one vercel.json wherever it next sits.
        "Add this to the `headers` array in the site's vercel.json:",
        "",
        json.dumps(vercel_headers(plan), indent=2),
        "",
        "Why each cluster:",
    ]
    for cluster, why in REMOVE.items():
        lines.append(f"  noindex  /{cluster}/   {why}")
    for cluster, why in KEEP.items():
        lines.append(f"  keep     /{cluster}/   {why}")
    if plan.survivors:
        lines += [
            "",
            f"{len(plan.survivors)} pages inside the removed clusters still earn impressions.",
            "They are worth a second look before this ships — a page with demand may deserve",
            "rewriting into something real rather than being switched off:",
        ]
        lines += [f"  {im:>5.0f} imp  {url}" for url, im in plan.survivors[:12]]
    lines += [
        "",
        "Do NOT add these paths to robots.txt Disallow. Google has to crawl a page to see the",
        "noindex header; blocking it in robots.txt preserves the index entry instead of removing it.",
    ]
    return "\n".join(lines)
