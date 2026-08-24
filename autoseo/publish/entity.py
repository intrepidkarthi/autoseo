"""Make DailyVox resolvable as one entity, on the pages that earn the impressions.

The AEO panel asks fifty buyer questions and records who gets named. Today the answer is Day One,
Rosebud and Apple Journal, and one reason is upstream of anything the composer can write: an engine
deciding whether to name an app has to first be sure which app it is looking at, and the site gives
it almost nothing to work with.

What is actually on the site, measured rather than assumed:

  - The **homepage** carries a `SoftwareApplication` node with `sameAs`, and that list holds two
    URLs — the App Store listing and the GitHub repo. The five owned social channels are absent.
    The homepage is also outside `site.writable_prefixes()`, so this module cannot fix it and does
    not try.
  - The **142 blog pages** carry `BlogPosting` with `publisher` and `author` inlined as
    `{"@type": "Organization", "name": "DailyVox", "url": "..."}` — a name and a URL, no `@id`, no
    `sameAs`. Nothing connects those pages to the App Store listing, the YouTube channel or the
    Reddit account, and these are the pages that earn every impression the blog gets.
  - The **blog index** carries no structured data at all. Zero `ld+json` blocks on the hub page
    that links the whole corpus.

So the entity is asserted once, weakly, on the one page this bot may not touch, and not at all on
the 142 it may. This module fixes the half that is in reach: it emits one `Organization` node
carrying the full `sameAs` graph, and rewires each page's existing `BlogPosting` to reference that
node by `@id` instead of restating a thinner copy of it.

Three deliberate choices:

  **`Organization` only — no second `SoftwareApplication`.** The tempting move is to copy the
  homepage's app node onto every article, and it is a trap. That node carries `softwareVersion`,
  and putting a version string on 142 pages creates 142 places for it to go stale. This repo has
  already been bitten by exactly that: `llms.txt` announced v1.4.1 while the app had shipped six
  minor versions past it, and nothing anywhere compared the two (see `agent_layer.audit_profile`).
  One authority per fact. The homepage owns the app node and its version; this owns the
  organisation and its channels, and every field on it is stable.

  **`@id` references, not inlined duplicates.** `sameAs` appears once per page, on the Organization
  node, and `BlogPosting.author` / `.publisher` become `{"@id": ...}` pointing at it. Inlining the
  full organisation into all three places would be the same fact stated three times per page and
  1,278 times across the blog, with three chances to disagree.

  **The `Blog` node states no title or description.** It would be the natural place for them and
  they are omitted on purpose: the head already carries both, `page.retitle` rewrites the head, and
  a copy here would silently disagree with it the first time a listing page is retitled. The node's
  job is linkage — this listing belongs to that organisation — not description.
"""

from __future__ import annotations

import json
import re

from autoseo.core.config import settings
from autoseo.core.log import get_logger
from autoseo.publish import agent_layer, site

log = get_logger(__name__)

BRAND = "DailyVox"

# The owned channels, spelled exactly as they are. Copy them; never derive one from another.
#
# The handles are inconsistent and that is not a typo waiting to be tidied: `getDailyVox` on X and
# Reddit, `getdailyvox` on Instagram, `dailyvoxapp` on YouTube, `DailyVox` on Quora. `sameAs` is a
# claim that these profiles are this organisation, and a claim pointing at a 404 is worse than no
# claim at all — it is an unverifiable assertion repeated on every page of the blog, made by a site
# whose entire positioning is that it does not overstate things.
#
# Every URL here was fetched and returns 200, except Quora, which returns 403 to any non-browser
# client. That is Quora refusing automation, not a missing profile — the account is ours. It stays,
# because an inert link costs nothing, and it is called out here so that nobody later "fixes" a 403
# by deleting a real channel.
SAME_AS: tuple[str, ...] = (
    "https://apps.apple.com/app/id6760454642",
    "https://github.com/intrepidkarthi/dailyvox",
    "https://x.com/getDailyVox",
    "https://www.instagram.com/getdailyvox/",
    "https://www.youtube.com/@dailyvoxapp",
    "https://www.reddit.com/user/getDailyVox/",
    "https://www.quora.com/profile/DailyVox",
)


def _base() -> str:
    return settings.site.rstrip("/")


def org_id() -> str:
    """The stable identifier every other node points at.

    A fragment on the site root rather than a bare URL: `https://getdailyvox.com/` already
    identifies the homepage as a `WebPage`, and reusing it for the organisation would make two
    different things share one name.
    """
    return f"{_base()}/#organization"


def organization() -> dict:
    return {
        "@type": "Organization",
        "@id": org_id(),
        "name": BRAND,
        "url": f"{_base()}/",
        "logo": {"@type": "ImageObject", "url": f"{_base()}/app-icon.png"},
        "sameAs": list(SAME_AS),
    }


# --- reading and writing the block ---------------------------------------------------------------

_LD = re.compile(r'([ \t]*)<script type="application/ld\+json">(.*?)</script>', re.S | re.I)
_HEAD = re.compile(r"([ \t]*)</head>", re.I)
_CANONICAL = re.compile(r'<link\s+rel="canonical"\s+href="([^"]+)"', re.I)


def _script(data: dict, indent: str) -> str:
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return f'{indent}<script type="application/ld+json">\n{body}\n{indent}</script>'


def _parse(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_ours(raw: str) -> bool:
    """Our block is the one whose @graph contains the organisation node.

    Identified by content rather than by a comment marker or by position, because `page.retitle`
    and `page.append_faq` both rewrite blocks in this head and neither preserves anything outside
    the JSON.

    A block that names the organisation but does not parse still counts as ours, and that fallback
    is the point rather than a nicety. Recognition failure here does not mean "skip the page" — it
    means `insert` finds nothing to refresh and adds a *second* block, leaving the page asserting
    two organisations, one of them corrupt. Claiming the broken one and overwriting it is the only
    outcome that repairs rather than compounds. Nothing in this repo can produce that state — every
    writer here parses before it serialises — but a hand edit in the site repo can, and this runs
    unattended over 142 pages with nobody reading the diff.
    """
    if (data := _parse(raw)) is not None:
        graph = data.get("@graph")
        if not isinstance(graph, list):
            return False
        return any(isinstance(n, dict) and n.get("@id") == org_id() for n in graph)
    return org_id() in raw


def _has_blogposting(doc: str) -> bool:
    return any(
        (d := _parse(m.group(2))) is not None and d.get("@type") == "BlogPosting"
        for m in _LD.finditer(doc)
    )


def present(doc: str) -> bool:
    return any(_is_ours(m.group(2)) for m in _LD.finditer(doc))


def _link_blogposting(doc: str) -> str:
    """Point the page's existing BlogPosting at the organisation node.

    Parsed as JSON and re-serialised rather than patched with a regex, for the reason
    `page._update_jsonld` gives: a mangled JSON-LD block is worse than an untouched one, and a
    parse failure here has to leave the block exactly as it was found.
    """
    ref = {"@id": org_id()}

    def replace(match: re.Match[str]) -> str:
        data = _parse(match.group(2))
        if data is None or data.get("@type") != "BlogPosting":
            return match.group(0)
        if data.get("author") == ref and data.get("publisher") == ref:
            return match.group(0)
        data["author"] = ref
        data["publisher"] = ref
        return _script(data, match.group(1))

    return _LD.sub(replace, doc)


def _graph(doc: str) -> list[dict]:
    graph: list[dict] = [organization()]
    if _has_blogposting(doc):
        return graph
    # A listing page: the index or one of the paginated pages. Its canonical URL is the only
    # per-page fact the node needs, and a page without one gets the organisation alone rather than
    # a Blog node with an invented @id.
    if canonical := _CANONICAL.search(doc):
        url = canonical.group(1)
        graph.append({
            "@type": "Blog",
            "@id": f"{url}#blog",
            "url": url,
            "publisher": {"@id": org_id()},
            "inLanguage": "en",
        })
    return graph


def insert(doc: str) -> str:
    """Add or refresh the entity block, and link the page's BlogPosting to it.

    Refreshes rather than skips when a block is already there. `agent_layer` learned this the
    expensive way: keying idempotency on "is it present" is correct for not duplicating and useless
    for fixing, and it froze 142 live pages with the first version of a block that had a layout bug.
    `sameAs` will gain a channel eventually, and when it does every page has to be able to take it.

    Returns the document unchanged when nothing needs doing, so `site.commit` drops it.
    """
    updated = _link_blogposting(doc)
    block = {"@context": "https://schema.org", "@graph": _graph(doc)}

    for match in _LD.finditer(updated):
        if not _is_ours(match.group(2)):
            continue
        rendered = _script(block, match.group(1))
        if match.group(0) == rendered:
            return updated
        return updated[:match.start()] + rendered + updated[match.end():]

    head = _HEAD.search(updated)
    if not head:
        raise RuntimeError("no </head> in this page — refusing to guess where the block belongs")
    rendered = _script(block, head.group(1))
    return updated[:head.start()] + rendered + "\n" + updated[head.start():]


# --- shipping it ---------------------------------------------------------------------------------

def backfill(dry_run: bool = False) -> str:
    """Put the entity block on every blog page, in one commit.

    One commit for the same reason `agent_layer.backfill` uses one: this is a single decision
    applied uniformly, so it should be a single thing to revert.

    A command, deliberately not one of the structural fixes `act.apply.run` checks every morning.
    Those three — delist, relink, the IndexNow key — are cheap comparisons against the live repo.
    This one reads all 142 pages, and after the first run the only pages that need it are ones the
    renderer just emitted, which already get it in `blog.render`. Putting it in the daily loop
    would spend 142 API calls a day to catch a state that only a hand edit in the site repo can
    produce. `agent_layer.backfill` is a command for exactly the same reason; if that ever changes,
    both should move together.
    """
    files: dict[str, str] = {}
    skipped: list[str] = []

    for path in agent_layer.pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        try:
            updated = insert(doc)
            if updated != doc:
                files[path] = updated
        except RuntimeError as exc:
            # One malformed page must not block the other 141. Recorded, not swallowed.
            skipped.append(f"{path}: {exc}")

    for note in skipped:
        log.warning("skipped %s", note)

    if not files:
        print("  Every blog page already carries the entity block.")
        return ""

    print(f"\n  {len(files)} page(s) gain or refresh the entity block:")
    for path in sorted(files)[:8]:
        print(f"      {path}")
    if len(files) > 8:
        print(f"      ... and {len(files) - 8} more")
    if skipped:
        print(f"  {len(skipped)} page(s) skipped — no </head> to insert into:")
        for note in skipped[:5]:
            print(f"      {note}")

    return site.commit(
        files,
        f"seo: identify DailyVox as one entity across the blog\n\n"
        f"{len(files)} page(s) gain an Organization node carrying the full sameAs graph — App "
        f"Store, GitHub, X, Instagram, YouTube, Reddit, Quora — and their existing BlogPosting "
        f"now references it by @id instead of restating a thinner copy.\n\n"
        f"Before this, the only sameAs on the site was on the homepage, listing two URLs, and the "
        f"142 pages that earn every impression the blog gets asserted nothing beyond a publisher "
        f"name. An answer engine deciding whether to name a journaling app has to know which app "
        f"it is looking at first; five owned channels it could not connect to this domain were "
        f"evidence it never saw.\n\n"
        f"Organization only. The homepage keeps the SoftwareApplication node and its version "
        f"string — copying that onto 142 pages would be 142 places for a version to go stale, "
        f"which is the failure llms.txt already had.",
        dry_run=dry_run,
    )


def status() -> tuple[int, int, int]:
    """(carrying the block, BlogPostings linked by @id, total). Read-only.

    Two numbers rather than one, because they can disagree: a page can carry the organisation node
    while its BlogPosting still inlines its own publisher, and that half-applied state is exactly
    what a partial backfill leaves behind.
    """
    ref = {"@id": org_id()}
    total = carrying = linked = 0
    for path in agent_layer.pages():
        doc = site.read_text(path)
        if doc is None:
            continue
        total += 1
        carrying += present(doc)
        for match in _LD.finditer(doc):
            data = _parse(match.group(2))
            if data and data.get("@type") == "BlogPosting" and data.get("publisher") == ref:
                linked += 1
                break
    return carrying, linked, total
