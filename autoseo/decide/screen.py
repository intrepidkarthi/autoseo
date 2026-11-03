"""Which outreach targets can actually list us: the competitor filter.

Added 2026-11-03. Of 389 outreach targets, all still `new`, the top of the list was mostly pages
no email will move: Rosebud's, Lound's and Memexlab's own "best journaling apps" posts, Reddit
threads, GitHub repos of rival apps, and Gemini grounding redirects that had expired. A vendor's
roundup ranks its own app first and exists to keep rivals off it; asking to be added is a wasted
email. The independent pages, about 145 domains, were buried under them.

Every target gets one kind:

    independent   a publisher or a writer who could add us. The only kind worth pitching.
    vendor        an app in our space selling itself. Known competitors by host
                  (`pitch.COMPETITORS`, `diagnose.COMPETITORS`), and any other site whose homepage
                  is about journaling, mood, stress or the like and links its own app on the App
                  Store or Google Play. That second test is what catches the vendors nobody has
                  listed yet (Anticipate, a burnout app, was the first); there is a new one every
                  month.
    community     Reddit, Quora, Hacker News, GitHub. Not pitchable, and Reddit links did not
                  show up for DailyVox anyway (Karthik, 2026-11-03), so not worth working.
    unresolved    still a grounding redirect: no page to read, no author to find.

Authors on Medium, Substack and the like are judged as authors: the platform's own app says
nothing about the writer, so those are independent without a homepage check.

A homepage that cannot be read is `independent`, not `vendor`. The list exists to find emails
worth sending, and hiding a real publisher because its homepage timed out costs more than
showing one vendor too many.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlsplit

from autoseo.decide import outreach, pitch
from autoseo.decide.diagnose import COMPETITORS as _DIAGNOSED

INDEPENDENT, VENDOR, COMMUNITY, UNRESOLVED, SKIP = (
    "independent", "vendor", "community", "unresolved", "skip")

COMMUNITY_HOSTS = ("reddit.com", "quora.com", "news.ycombinator.com", "github.com",
                   "stackexchange.com", "stackoverflow.com")
PLATFORMS = ("medium.com", "substack.com", "dev.to", "hashnode.dev", "wordpress.com",
             "blogspot.com", "tumblr.com", "linkedin.com", "notion.site")

# The diagnosis tags EmpathDash a competitor for citation purposes, and it is one there, but it is
# therapy-practice software writing roundups for clients, not a journal app: an editor can add us.
_KNOWN = tuple(sorted(set(_DIAGNOSED) - {"empathdash.com"}))

_STORE_LINK = re.compile(r"apps\.apple\.com/[^\"'\s]*app/|itunes\.apple\.com/[^\"'\s]*app/|"
                         r"play\.google\.com/store/apps/details")
_OUR_SPACE = re.compile(r"journal|diary|diaries|mood|stress|burnout|anxiety|therap|wellness|"
                        r"wellbeing|mindful|habit|reflect|self-care", re.I)
_HEAD = re.compile(r"<title[^>]*>(.*?)</title>|<meta[^>]+name=[\"']description[\"'][^>]*>|"
                   r"<h1[^>]*>(.*?)</h1>", re.I | re.S)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _under(host: str, roots: tuple[str, ...]) -> bool:
    return any(host == r or host.endswith("." + r) for r in roots)


def sells_a_journal_app(html: str) -> bool:
    """A homepage in our space that links an app of its own in a store.

    Both, never one. Tom's Guide links the App Store from reviews and is not a vendor; Holstee
    sells paper journals and is not one either.
    """
    head = " ".join(m.group(0) for m in _HEAD.finditer(html[:200_000]))
    return bool(_STORE_LINK.search(html)) and bool(_OUR_SPACE.search(head))


def kind(domain: str, url: str,
         fetcher: Callable[[str], pitch.Fetched | None] = pitch.fetch) -> tuple[str, str]:
    """(kind, reason) for one target. At most one homepage fetch, cached for a week."""
    if outreach.is_skipped(domain, url):
        return SKIP, "app store, social network, or us"
    if "vertexaisearch" in url:
        return UNRESOLVED, "grounding redirect that no longer resolves"
    host = _host(url) or (domain or "").lower()
    if _under(host, COMMUNITY_HOSTS) or _under(domain, COMMUNITY_HOSTS):
        return COMMUNITY, "forum or code host"
    vendor = pitch.vendor_of(host) or pitch.vendor_of(domain)
    if vendor:
        return VENDOR, f"{vendor}'s own site"
    if _under(host, _KNOWN) or _under(domain, _KNOWN):
        return VENDOR, "a competitor the diagnosis already tracks"
    if _under(host, PLATFORMS):
        return INDEPENDENT, "a writer on a publishing platform"
    got = fetcher(f"https://{host}/")
    if got is not None and got.status == 200 and sells_a_journal_app(got.html):
        return VENDOR, "homepage is about journaling and links its own app in a store"
    return INDEPENDENT, "publisher" if got is not None and got.status == 200 else "homepage unread"
