"""Edit the site's sitemaps.

A sitemap is a set of assertions: every URL in it is a claim that the page is worth indexing. Three
of those claims are currently wrong — a `/reports/` URL that returns 404, and six `/blog/page/N`
listings that are navigation rather than content — and a wrong claim is a reason for Google to trust
the rest of the file less.

`sitemap-articles.xml` is regenerated wholesale by the renderer and its `<url>` elements are not
touched here. `sitemap-blog.xml` (133 URLs) and `sitemap-core.xml` are hand-maintained files that
nothing in the site repo generates, so they are edited surgically: drop the named `<url>` elements,
leave every byte around them alone.

`sitemap.xml` — the index — is a fourth file and a different job. It carries one `<lastmod>` per
child, and Google reads those to decide whether re-fetching a child is worth the crawl. Nothing
wrote to it for the first two weeks of the autonomous loop, so the index went on advertising
`sitemap-articles.xml` as unchanged since 2026-06-16 while that file was in fact being rewritten
every night. Whoever changes a child now stamps the index in the same commit — see `index_update`.
"""

from __future__ import annotations

import datetime as dt
import re

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

# The sitemaps this module may edit. `sitemap-articles.xml` is deliberately absent: it is generated
# from the markdown on every publish, so an edit here would be overwritten by the next post.
#
# A function rather than a dict literal because resolving the website root costs an API call, and
# this module is imported by the `plan` job, which holds no token for the site repo. Built at import
# time it would fail the whole planning run to answer a question planning never asks — `plan` only
# uses `urls()`, which parses a string it fetched over HTTP.
def editable() -> dict[str, str]:
    root = site.site_dir()
    return {
        "blog": f"{root}/public/sitemap-blog.xml",
        "core": f"{root}/public/sitemap-core.xml",
    }

_URL_BLOCK = re.compile(r"[ \t]*<url>.*?</url>[ \t]*\n?", re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_SITEMAP_BLOCK = re.compile(r"[ \t]*<sitemap>.*?</sitemap>[ \t]*\n?", re.S)
_LASTMOD = re.compile(r"<lastmod>\s*([^<\s]+)\s*</lastmod>")


def index_path() -> str:
    return f"{site.site_dir()}/public/sitemap.xml"


def read(name: str) -> str | None:
    return site.read_text(editable()[name])


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


def _stamp(block: str, day: str) -> str:
    """Set `<lastmod>` on one `<url>`/`<sitemap>` element, adding the tag if it has none."""
    if _LASTMOD.search(block):
        return _LASTMOD.sub(f"<lastmod>{day}</lastmod>", block, count=1)
    return block.replace("</loc>", f"</loc><lastmod>{day}</lastmod>", 1)


def lastmods(xml: str) -> dict[str, str]:
    """{url: lastmod} for every `<url>` that declares one."""
    found: dict[str, str] = {}
    for m in _URL_BLOCK.finditer(xml):
        loc, lm = _LOC.search(m.group(0)), _LASTMOD.search(m.group(0))
        if loc and lm:
            found[loc.group(1)] = lm.group(1)
    return found


def with_lastmod(xml: str, day: str, fresh: set[str], carry: dict[str, str]) -> str:
    """Give every `<url>` in a urlset a `<lastmod>`: `day` for anything in `fresh`, else whatever
    `carry` remembers for it, else `day`.

    The vendored renderer emits `<changefreq>` and `<priority>` — the two elements Google has said
    it ignores — and omits the one it reads. Rather than edit `vendor/render_articles.py`, which is
    kept re-copyable byte-for-byte from the site repo, the tag is added to its output here. Same
    reason `entity` and `agent_layer` decorate the rendered HTML instead of the renderer.

    `carry` matters: rebuilding the sitemap from markdown every night would otherwise re-date all
    twenty-odd articles on the day any one of them changed, which tells Google the opposite of the
    truth about which page is worth re-crawling.
    """
    def stamp(m: re.Match[str]) -> str:
        loc = _LOC.search(m.group(0))
        if not loc:
            return m.group(0)
        url = loc.group(1)
        return _stamp(m.group(0), day if url in fresh else carry.get(url, day))

    return _URL_BLOCK.sub(stamp, xml)


def touch_index(xml: str, children: set[str], day: str) -> tuple[str, list[str]]:
    """Re-date the named child sitemaps in a sitemap index. `children` are bare filenames."""
    changed: list[str] = []

    def stamp(m: re.Match[str]) -> str:
        loc = _LOC.search(m.group(0))
        if not loc or loc.group(1).rsplit("/", 1)[-1] not in children:
            return m.group(0)
        changed.append(loc.group(1))
        return _stamp(m.group(0), day)

    return _SITEMAP_BLOCK.sub(stamp, xml), changed


def index_update(children: set[str], day: str | None = None) -> dict[str, str]:
    """{path: xml} re-dating `children` in the sitemap index, or {} if there is nothing to change.

    Returned as a file map so callers fold it into the commit that changed the child, rather than
    landing a second commit that says the child changed at a moment when it did not.
    """
    day = day or dt.date.today().isoformat()
    xml = site.read_text(index_path())
    if xml is None:
        # Not fatal: a stale index slows discovery, it does not break anything, and failing the
        # publish over it would be a worse trade than shipping the page with an old date.
        log.warning("%s not found — child sitemap dates will stay stale", index_path())
        return {}
    updated, changed = touch_index(xml, children, day)
    if not changed or updated == xml:
        return {}
    log.info("sitemap index: re-dated %s to %s", ", ".join(sorted(changed)), day)
    return {index_path(): updated}


def drop_urls(targets: set[str], rationale: str, dry_run: bool = False) -> str:
    """Remove URLs from whichever sitemaps carry them. One commit for all of them."""
    normalised = {u.rstrip("/") for u in targets}
    files: dict[str, str] = {}
    removed: list[str] = []

    for path in editable().values():
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

    # In the same commit as the edit, so the index never claims a date the child did not change on.
    files |= index_update({path.rsplit("/", 1)[-1] for path in files})

    listed = "\n".join(f"  {u}" for u in sorted(removed)[:20])
    return site.commit(
        files,
        f"seo: drop {len(removed)} URL(s) from the sitemaps\n\n{rationale}\n\n{listed}",
        dry_run=dry_run,
    )
