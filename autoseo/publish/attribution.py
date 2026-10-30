"""Tag every store link with the page it sits on, so installs can be traced to the page that sent them.

Added 2026-10-30. Ninety days of Search Console said 301 clicks, and nothing anywhere said how many
of those became an install — so no decision the loop made could be judged on the thing that
matters. Both stores attribute a download to the link that led to it, with no tracking on the site:

  App Store  ?pt=<provider token>&ct=<campaign>&mt=8  App Store Connect > Analytics > Sources
  Play       &referrer=utm_source=...&utm_campaign=   Play Console > Acquisition reports

The privacy promise is untouched. Nothing runs in the reader's browser; the store sees which link
was followed, exactly as it already sees that a web link was followed.

The App Store needs the team's provider token, which only App Store Connect shows. Until
AUTOSEO_APPSTORE_PT is set the links carry the campaign without it — App Store Connect ignores
`ct` without `pt`, so nothing is attributed yet, but every link is ready and the token is added on
the next pass. Play attribution works from the first commit.

The one rule that makes it safe to let this touch the home page: an edit may change store-link URLs
inside `href` attributes and nothing else. Every rewrite is checked by blanking those URLs in the
old and new page and requiring the two to be identical; anything else is refused.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from autoseo.core.config import settings
from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

APP_ID = "6760454642"
PACKAGE = "com.dailyvox.app"

# Store URLs inside an href attribute only. A JSON-LD `installUrl` or a URL in visible text is
# left alone: `&amp;` is right in an attribute and wrong in JSON.
_HREF = re.compile(
    r'href="(https://(?:apps|itunes)\.apple\.com/[^"]*id' + APP_ID + r'[^"]*'
    r'|https://play\.google\.com/store/apps/details\?id=' + re.escape(PACKAGE) + r'[^"]*)"')

# Top-level pages with store links (29 Oct crawl). Exact names, added to the writable paths for
# this module's edits; see `site._writable`.
CORE_PAGES = ("index", "about", "ai-journaling-app", "changelog", "compare", "demo", "facts", "faq",
              "for-students", "glossary", "personal-digital-twin", "press", "privacy", "research",
              "research-statement", "sky")


def campaign(path: str) -> str:
    """A short campaign name for a page: 'home', 'c-faq', 'b-best-voice-journal-app'.

    App Store Connect keeps 40 characters of a campaign name, so long blog slugs are cut there.
    """
    path = path.strip("/").removesuffix(".html")
    if path in ("", "index"):
        return "home"
    if path.startswith("blog/"):
        return ("b-" + path.removeprefix("blog/"))[:40].rstrip("-")
    return ("c-" + path.replace("/", "-"))[:40].rstrip("-")


def store_url(url: str, camp: str) -> str:
    """The same store link, carrying this page's campaign. Idempotent."""
    if "apple.com" in url:
        pt = settings.appstore_provider_token
        params = (f"pt={pt}&amp;" if pt else "") + f"ct={camp}&amp;mt=8"
        return f"https://apps.apple.com/app/id{APP_ID}?{params}"
    referrer = quote(f"utm_source=getdailyvox.com&utm_medium=web&utm_campaign={camp}", safe="")
    return f"https://play.google.com/store/apps/details?id={PACKAGE}&amp;referrer={referrer}"


def _blank(doc: str) -> str:
    return _HREF.sub('href="STORE"', doc)


def tag(doc: str, path: str) -> str:
    """The page with every store link tagged for `path`. Raises if anything else would change."""
    camp = campaign(path)
    updated = _HREF.sub(lambda m: f'href="{store_url(m.group(1), camp)}"', doc)
    if _blank(updated) != _blank(doc):
        raise RuntimeError(f"{path}: attribution would change more than store links — refusing")
    return updated


def pages() -> list[str]:
    """Repo paths of every page this tags: the top-level pages and every blog page."""
    root = site.site_dir()
    blog = [f"{root}/public/blog/{e['name']}" for e in site.list_dir(f"{root}/public/blog")
            if e["type"] == "file" and e["name"].endswith(".html")]
    blog += [f"{root}/public/blog/page/{n}.html" for n in range(2, 8)]
    return [*(f"{root}/public/{p}.html" for p in CORE_PAGES), *blog]


def path_of(repo_path: str) -> str:
    """'website/public/blog/x.html' -> '/blog/x'."""
    return "/" + repo_path.split("/public/", 1)[1].removesuffix(".html")


def backfill(dry_run: bool = False) -> str:
    """Tag every page that is not tagged yet, in one commit. A no-op once the site is done."""
    from concurrent.futures import ThreadPoolExecutor

    paths = pages()
    with ThreadPoolExecutor(max_workers=6) as pool:
        docs = dict(zip(paths, pool.map(site.read_text, paths), strict=True))
    files: dict[str, str | None] = {}
    for path, doc in docs.items():
        if doc is None:
            continue
        updated = tag(doc, path_of(path))
        if updated != doc:
            files[path] = updated
    if not files:
        return ""
    token = "with" if settings.appstore_provider_token else "without (set AUTOSEO_APPSTORE_PT)"
    return site.commit(
        files,
        f"seo: tag store links with the page they sit on ({len(files)} page(s))\n\n"
        f"Every App Store and Play link now names its page as a campaign, so App Store Connect "
        f"and Play Console can attribute installs to it. App Store links are {token} the "
        f"provider token. Nothing runs in the reader's browser.",
        dry_run=dry_run, verified=True,
    )
