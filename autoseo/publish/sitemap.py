"""Edit the site's sitemaps.

A sitemap is a set of assertions: every URL in it is a claim that the page is worth indexing. Three
of those claims are currently wrong — a `/reports/` URL that returns 404, and six `/blog/page/N`
listings that are navigation rather than content — and a wrong claim is a reason for Google to trust
the rest of the file less.

`sitemap-articles.xml` is regenerated wholesale by the renderer and is not touched here.
`sitemap-blog.xml` (133 URLs) and `sitemap-core.xml` are hand-maintained files that nothing in the
site repo generates, so they are edited surgically: drop the named `<url>` elements, leave every
byte around them alone.
"""

from __future__ import annotations

import re

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

# The sitemaps this module may edit. `sitemap-articles.xml` is deliberately absent: it is generated
# from the markdown on every publish, so an edit here would be overwritten by the next post.
EDITABLE = {
    "blog": f"{site.SITE_DIR}/public/sitemap-blog.xml",
    "core": f"{site.SITE_DIR}/public/sitemap-core.xml",
}

_URL_BLOCK = re.compile(r"[ \t]*<url>.*?</url>[ \t]*\n?", re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def read(name: str) -> str | None:
    return site.read_text(EDITABLE[name])


def remove(xml: str, urls: set[str]) -> tuple[str, list[str]]:
    """Drop the `<url>` elements naming any of `urls`. Returns the new XML and what went."""
    removed: list[str] = []

    def drop(match: re.Match[str]) -> str:
        loc = _LOC.search(match.group(0))
        if loc and loc.group(1).rstrip("/") in urls:
            removed.append(loc.group(1))
            return ""
        return match.group(0)

    return _URL_BLOCK.sub(drop, xml), removed


def urls(xml: str) -> list[str]:
    return _LOC.findall(xml)


def drop_urls(targets: set[str], rationale: str, dry_run: bool = False) -> str:
    """Remove URLs from whichever sitemaps carry them. One commit for all of them."""
    normalised = {u.rstrip("/") for u in targets}
    files: dict[str, str] = {}
    removed: list[str] = []

    for path in EDITABLE.values():
        xml = site.read_text(path)
        if xml is None:
            log.warning("%s not found — skipping", path)
            continue
        updated, gone = remove(xml, normalised)
        if gone:
            files[path] = updated
            removed += gone

    if not files:
        return ""

    listed = "\n".join(f"  {u}" for u in sorted(removed)[:20])
    return site.commit(
        files,
        f"seo: drop {len(removed)} URL(s) from the sitemaps\n\n{rationale}\n\n{listed}",
        dry_run=dry_run,
    )
