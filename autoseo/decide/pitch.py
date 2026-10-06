"""Draft pitches for the roundups answer engines cite, and check the listings already won.

`outreach.py` ranks the pages worth being on. That list sat at 219 rows, every one of them `new`,
because a ranked URL is not yet an email: someone has to open the page, see what it covers, find
the reason an editor would bother, and write it up. Most of that is reading, and reading is
mechanical. This does the reading and leaves the writing.

For each of the top roundup-shaped targets it fetches the page once a week (cached, so a re-run
does not refetch), and records:

    whether DailyVox is already on it    the name, getdailyvox.com, App Store id 6760454642, or the
                                         Play id com.dailyvox.app
    which competitors it names           and the links it gives each of them
    which of those links are dead        404/410, a domain that no longer resolves, or a parking
                                         page. A dead entry is the best opening there is: the
                                         editor's page is broken, and the pitch fixes it

Conservative where it matters. A timeout, a 403 and a 5xx are *unknown*, never dead. Telling an
editor their link is broken when it is not costs the one email that was going to be sent.

The draft is a deterministic template filled with checked facts, and it goes through the same
write-like-me gate as everything else this repo produces. No model writes any of it. Every file
opens by saying it is a draft for Karthik to rewrite, because a pitch that reads like a template is
one an editor deletes, and autoseo sends nothing.

`review` is the Monday check. A page that listed us can drop us in an edit and nobody would know;
the outreach row keeps `listed` and its date forever by design. So listed pages are re-read and any
that no longer name or link DailyVox are reported. States are never changed here: `listed` belongs
to measurement in `outreach.record`, the rest to a person via `autoseo outreach --mark`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import socket
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from autoseo.core.config import settings
from autoseo.core.log import get_logger
from autoseo.decide import outreach

log = get_logger(__name__)

CACHE_DAYS = 7
TIMEOUT = 15.0
WORKERS = 6
MAX_LINKS_CHECKED = 40          # per page; a roundup of forty apps is already an outlier
USER_AGENT = "Mozilla/5.0 (compatible; autoseo/0.1; +https://github.com/intrepidkarthi/autoseo)"

HEADER = "DRAFT for Karthik to rewrite in his own words before sending. autoseo never sends."

# Threads and repos are not pages an author updates on request. Reddit is manual by policy anyway.
NOT_PITCHABLE = {"reddit.com", "quora.com", "news.ycombinator.com", "github.com"}

# --- who we are -----------------------------------------------------------------------------------

APP_STORE_ID = "6760454642"
PLAY_ID = "com.dailyvox.app"
_OUR_NAME = re.compile(r"\bdaily\s?vox\b", re.I)
_OUR_LINKS = ("getdailyvox.com", f"id{APP_STORE_ID}", PLAY_ID)

# The only claims a draft may make. Each one is checkable by the editor in under a minute, and none
# of them is an adjective.
FACTS = (
    "DailyVox is a free voice journal for iPhone and Android. The Android app needs Android 13 or "
    "later and has been on Google Play since October 2026. Transcription runs on the device. There "
    "is no account and no server.\n\n"
    "On Android the app requests no internet permission. Anyone can check that in Settings › Apps › "
    "DailyVox › Permissions. On the App Store its privacy label reads \"Data Not Collected\".\n\n"
    "The app source is MIT licensed on GitHub. The Twin engine is proprietary.\n\n"
    "- https://getdailyvox.com\n"
    f"- https://apps.apple.com/app/id{APP_STORE_ID}\n"
    f"- https://play.google.com/store/apps/details?id={PLAY_ID}"
)

BLURB = (
    "DailyVox (free, iPhone and Android). A voice journal that transcribes on the device, with no "
    "account and no server. On Android it does not ask for internet access at all."
)

# --- who they name ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Competitor:
    name: str
    hosts: tuple[str, ...] = ()
    # A name that is also an ordinary word ("Journey", "Stoic") only counts when the page links to
    # the product. Otherwise every sentence starting "Journey through your thoughts" is a listing.
    ambiguous: bool = False


COMPETITORS = (
    Competitor("Day One", ("dayoneapp.com",)),
    Competitor("Apple Journal"),
    Competitor("Rosebud", ("rosebud.app",)),
    Competitor("Reflectly", ("reflectly.app",)),
    Competitor("Journey", ("journey.cloud",), ambiguous=True),
    Competitor("Daylio", ("daylio.net",)),
    Competitor("Diarium", ("diariumapp.com",)),
    Competitor("Penzu", ("penzu.com",)),
    Competitor("Stoic", ("getstoic.com",), ambiguous=True),
    Competitor("Otter", ("otter.ai",), ambiguous=True),
    Competitor("Mindsera", ("mindsera.com",)),
    Competitor("Reflection", ("reflection.app",), ambiguous=True),
    Competitor("Life Note", ("mylifenote.ai",)),
    Competitor("Lound", ("lound.ai",)),
    Competitor("Deepjournal", ("deepjournal.app",)),
    Competitor("AudioPen", ("audiopen.ai",)),
    Competitor("Voicenotes", ("voicenotes.com",)),
    Competitor("Grid Diary", ("griddiary.com",)),
    Competitor("Five Minute Journal", ("intelligentchange.com",)),
    Competitor("Momento", ("momento.app",), ambiguous=True),
    Competitor("JournalLM", ("journallm.app",)),
    Competitor("Speakwise", ("speakwiseapp.com",)),
    Competitor("Pensio", ("pensio.app",)),
    Competitor("Dayora", ("dayora.ai",)),
    Competitor("Journiv", ("journiv.com",)),
    Competitor("Memex", ("memexlab.ai",), ambiguous=True),
    Competitor("Reflect", ("reflect.app",), ambiguous=True),
    Competitor("Just Press Record", ("openplanetsoftware.com",)),
    Competitor("Voice Memos"),
)


def vendor_of(domain: str) -> str:
    """The competitor whose own site this is, or "". Their roundups rank themselves first."""
    d = domain.lower().removeprefix("www.")
    for c in COMPETITORS:
        if any(d == h or d.endswith("." + h) for h in c.hosts):
            return c.name
    return ""


# --- reading a page -----------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    status: int
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)      # (absolute href, anchor text)


class _Reader(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.title, self.description = "", ""
        self.headings: list[str] = []
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in: str | None = None
        self._buf: list[str] = []
        self._href: str | None = None
        self._anchor: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "meta" and (a.get("name") or a.get("property") or "").lower() in (
                "description", "og:description") and not self.description:
            self.description = a.get("content") or ""
        elif tag in ("title", "h1", "h2", "h3"):
            self._in, self._buf = tag, []
        elif tag == "a" and a.get("href"):
            self._href, self._anchor = urljoin(self.base, a["href"]), []

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == self._in:
            txt = " ".join("".join(self._buf).split())
            if tag == "title":
                self.title = self.title or txt
            elif txt:
                self.headings.append(txt)
            self._in = None
        elif tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._anchor).split())))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._in:
            self._buf.append(data)
        if self._href:
            self._anchor.append(data)


def parse(html: str, url: str, status: int = 200) -> Page:
    r = _Reader(url)
    try:
        r.feed(html)
    except Exception as exc:  # noqa: BLE001 — a malformed page still yields what was read
        log.info("partial parse of %s: %s", url, exc)
    return Page(url=url, status=status, title=r.title, description=r.description,
                headings=r.headings, text=" ".join(" ".join(r.text).split()), links=r.links)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def mentions_us(page: Page) -> list[str]:
    """Evidence that the page lists DailyVox, or [] if it does not."""
    found = []
    if _OUR_NAME.search(page.text) or _OUR_NAME.search(page.title):
        found.append("names DailyVox")
    hrefs = " ".join(h for h, _ in page.links).lower()
    for marker in _OUR_LINKS:
        if marker.lower() in hrefs:
            found.append(f"links {marker}")
    return found


def competitors_named(page: Page) -> dict[str, list[str]]:
    """{competitor: outbound links the page gives it}. A name with no link maps to []."""
    own = _host(page.url)
    external = [(h, t) for h, t in page.links
                if h.startswith("http") and _host(h) and _host(h) != own]
    vendor = vendor_of(own)
    lowered = page.text.lower()
    out: dict[str, list[str]] = {}
    for c in COMPETITORS:
        if c.name == vendor:
            continue                    # a vendor's blog naming itself is not a listing
        named = re.search(rf"\b{re.escape(c.name)}\b", page.text)
        # Written as its domain ("Otter.ai") the name is unambiguous even without a link.
        by_domain = any(h in lowered for h in c.hosts)
        links = sorted({h for h, t in external
                        if any(_host(h) == x or _host(h).endswith("." + x) for x in c.hosts)
                        or (t and re.search(rf"\b{re.escape(c.name)}\b", t))})
        if c.ambiguous and not (links or by_domain):
            continue
        if named or links or by_domain:
            out[c.name] = links
    return out


# --- fetching, with a week-long cache -----------------------------------------------------------

@dataclass
class Fetched:
    url: str
    status: int
    final_url: str
    html: str
    fetched_at: str


def _default_cache() -> Path:
    # state/tmp is gitignored: a page cache is not state, and committing third-party HTML to a public
    # repo would republish it.
    return settings.state_dir / "tmp" / "pages"


def _get(url: str) -> Fetched:
    r = httpx.get(url, timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": USER_AGENT})
    return Fetched(url=url, status=r.status_code, final_url=str(r.url), html=r.text[:2_000_000],
                   fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))


def fetch(url: str, max_age_days: float = CACHE_DAYS, cache_dir: Path | None = None,
          getter: Callable[[str], Fetched] | None = None) -> Fetched | None:
    """The page, from the cache if fetched within `max_age_days`, else over HTTP. None on failure.

    Only a 200 is cached. A 403 today may be a 200 tomorrow, and caching it would hide the page for
    a week.
    """
    cache_dir = cache_dir or _default_cache()
    path = cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()[:20]}.json"
    if path.exists() and max_age_days > 0:
        try:
            hit = Fetched(**json.loads(path.read_text(encoding="utf-8")))
            age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(hit.fetched_at)
            if age < dt.timedelta(days=max_age_days):
                return hit
        except (ValueError, TypeError, KeyError):
            pass
    try:
        got = (getter or _get)(url)
    except Exception as exc:  # noqa: BLE001 — an unreachable page is skipped, not fatal
        log.info("could not fetch %s: %s", url, type(exc).__name__)
        return None
    if got.status == 200:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got.__dict__), encoding="utf-8")
    return got


# --- is a competitor's link dead? ---------------------------------------------------------------

_PARKED_HOSTS = ("sedo.com", "dan.com", "afternic.com", "hugedomains.com", "bodis.com",
                 "parkingcrew.net", "above.com", "undeveloped.com", "parklogic.com")
_PARKED_TEXT = re.compile(
    r"this domain (?:name )?(?:is|may be) for sale|buy this domain|domain is parked|"
    r"parked (?:free|domain)|the domain [\w.-]+ (?:has expired|is expired)|"
    r"domain (?:has )?expired", re.I)


@dataclass
class LinkCheck:
    url: str
    verdict: str          # dead | live | unknown
    detail: str


def classify(status: int, final_url: str = "", body: str = "") -> tuple[str, str]:
    """dead | live | unknown for a completed response. Only certainty is dead."""
    if status in (404, 410):
        return "dead", f"HTTP {status}"
    if 200 <= status < 300:
        host = _host(final_url)
        if any(host == p or host.endswith("." + p) for p in _PARKED_HOSTS):
            return "dead", f"redirects to a domain parking page ({host})"
        if _PARKED_TEXT.search(body[:20_000]):
            return "dead", "serves a domain parking or for-sale page"
        return "live", f"HTTP {status}"
    return "unknown", f"HTTP {status}"


def _nxdomain(host: str) -> bool:
    """Does the name definitively not exist? A temporary resolver failure is not that."""
    try:
        socket.getaddrinfo(host, 443)
        return False
    except socket.gaierror as exc:
        return exc.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
    except OSError:
        return False


def check_link(url: str, client: httpx.Client,
               nxdomain: Callable[[str], bool] = _nxdomain) -> LinkCheck:
    try:
        r = client.get(url)
    except httpx.TimeoutException:
        return LinkCheck(url, "unknown", "timed out")
    except httpx.ConnectError:
        if nxdomain(_host(url)):
            return LinkCheck(url, "dead", "domain does not resolve")
        return LinkCheck(url, "unknown", "could not connect")
    except Exception as exc:  # noqa: BLE001 — anything else is not evidence of death
        return LinkCheck(url, "unknown", type(exc).__name__)
    verdict, detail = classify(r.status_code, str(r.url), r.text if r.status_code < 300 else "")
    return LinkCheck(url, verdict, detail)


def check_links(urls: list[str]) -> dict[str, LinkCheck]:
    urls = sorted(set(urls))[:MAX_LINKS_CHECKED]
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"user-agent": USER_AGENT}) as client:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return dict(zip(urls, pool.map(lambda u: check_link(u, client), urls), strict=True))


# --- the pitch ----------------------------------------------------------------------------------

@dataclass
class Pitch:
    url: str
    domain: str
    title: str
    covers: str
    citations: int
    mentioned: list[str]
    competitors: dict[str, list[str]]
    dead: list[tuple[str, LinkCheck]] = field(default_factory=list)
    unknown: int = 0
    checked: int = 0
    path: Path | None = None
    gate: str = ""

    @property
    def vendor(self) -> str:
        return vendor_of(self.domain)


def _clean(s: str, limit: int = 200) -> str:
    """Third-party text, made safe to quote: no dashes the house style forbids, no emoji, short."""
    s = s.replace("—", "-").replace("–", "-")
    s = "".join(ch for ch in s if ord(ch) < 0x2190 or ch == "›")
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return s


def _covers(page: Page) -> str:
    if page.description:
        return _clean(page.description, 220)
    subs = [h for h in page.headings[1:6] if len(h) < 90]
    if subs:
        return "Its sections include " + "; ".join(_clean(h, 80) for h in subs[:4])
    return "The page has no description or section headings to summarise"


def filename(domain: str, url: str) -> str:
    segs = [s for s in urlsplit(url).path.split("/") if s]
    tail = re.sub(r"[^a-z0-9]+", "-", (segs[-1] if segs else "home").lower()).strip("-")[:60]
    return f"{re.sub(r'[^a-z0-9.-]+', '-', domain.lower())}-{tail or 'home'}.md"


def render(p: Pitch) -> str:
    names = sorted(p.competitors)
    # Three headings, no more: the gate reads more than three in a piece this short as a template,
    # and it is right to.
    lines = [HEADER, "", f"Pitch for {p.domain}", "", "## The page", "",
             _clean(p.title, 160) or "(no title)", p.url, ""]
    named = (f"It names {len(names)} app(s) we track ({', '.join(names)})." if names
             else "It names none of the apps we track.")
    us = (f"The page already mentions DailyVox ({', '.join(p.mentioned)}). Read what it says "
          f"before writing anything." if p.mentioned else "It does not mention DailyVox.")
    lines += [f"{_clean(p.covers, 240).rstrip('.')}. {named} {us}", ""]
    if p.vendor:
        lines += [f"This is {p.vendor}'s own blog. A vendor ranks its own app first and rarely "
                  f"adds a rival, so this one is a long shot.", ""]
    if p.citations:
        lines += [f"Answer engines cited this page {p.citations} time(s) when asked the buyer "
                  f"questions DailyVox should answer.", ""]

    lines += ["## Opening", ""]
    if p.dead:
        for name, chk in p.dead:
            lines.append(f"- The {name} entry links to {chk.url}, which {_dead_phrase(chk)} when "
                         f"autoseo checked it.")
        lines += ["", "A broken entry is a fix the editor can make today, and DailyVox could fill "
                      "the slot.", ""]
    else:
        lines += ["No competitor link on the page is dead, so there is no broken entry to offer a "
                  "fix for. The pitch has to stand on the facts below.", ""]
    if p.unknown:
        lines += [f"{p.unknown} link(s) could not be checked (timeouts or blocked requests). They "
                  f"are not counted as dead.", ""]

    lines += ["## DailyVox", "", FACTS, "", "A blurb they could paste, if they want one.", "",
              BLURB, ""]
    return "\n".join(lines)


def _dead_phrase(chk: LinkCheck) -> str:
    if chk.detail.startswith("HTTP"):
        return f"returned {chk.detail}"
    return chk.detail.replace("does not", "did not").replace("serves", "served") \
                     .replace("redirects", "redirected")


def gated(text: str) -> tuple[str, str]:
    """The text after the write-like-me gate, and a one-line verdict."""
    from autoseo.quality import gate
    v = gate.evaluate(text, context="outreach", check_duplication=False)
    return v.text, v.summary()


# --- choosing targets ---------------------------------------------------------------------------

def _not_pitchable(domain: str) -> bool:
    d = domain.lower().removeprefix("www.")
    return any(d == x or d.endswith("." + x) for x in NOT_PITCHABLE)


def choose(top: int, rows: list[dict] | None = None,
           resolver: Callable[[str], str] = outreach.resolve) -> list[dict]:
    """The top `top` roundup-shaped targets still marked `new`, with their URLs resolved.

    Resolution happens here, lazily and only until `top` are found: most stored rows below the
    outreach shortlist still hold a grounding redirect, and some of those have expired.
    """
    rows = outreach.stored("new") if rows is None else rows
    out: list[dict] = []
    seen: set[str] = set()
    # Independent roundups first. A competitor's own "best alternatives" post names rivals to rank
    # above them, and is the least likely page on the list to add one more; it still gets a draft
    # when there are not enough independent pages to fill `top`.
    vendors: list[dict] = []
    for r in rows:
        if len(out) >= top:
            break
        if outreach.is_skipped(r["domain"], r["url"]) or _not_pitchable(r["domain"]):
            continue
        url = resolver(r["url"]) if "vertexaisearch" in r["url"] else r["url"]
        if "vertexaisearch" in url or url in seen:
            continue                     # an expired redirect, or a duplicate of a resolved row
        if outreach.is_skipped(r["domain"], url) or not outreach._is_listicle(r.get("title") or "", url):
            continue
        seen.add(url)
        (vendors if vendor_of(r["domain"]) else out).append({**r, "url": url})
    return (out + vendors)[:top]


def build(row: dict, page: Page, link_checker: Callable[[list[str]], dict[str, LinkCheck]]
          = check_links) -> Pitch:
    comps = competitors_named(page)
    checks = link_checker([u for links in comps.values() for u in links])
    dead = [(name, checks[u]) for name, links in sorted(comps.items()) for u in links
            if u in checks and checks[u].verdict == "dead"]
    unknown = sum(1 for c in checks.values() if c.verdict == "unknown")
    return Pitch(url=row["url"], domain=row["domain"], title=page.title, covers=_covers(page),
                 citations=int(row.get("citations") or 0), mentioned=mentions_us(page),
                 competitors=comps, dead=dead, unknown=unknown, checked=len(checks))


def write(p: Pitch, out_dir: Path) -> Path:
    text, verdict = gated(render(p))
    p.gate = verdict
    if not verdict.startswith("PASS"):
        text = text.replace(HEADER, f"{HEADER}\n\nThe write-like-me gate flagged this draft. "
                                    f"{verdict}", 1)
    out_dir.mkdir(parents=True, exist_ok=True)
    p.path = out_dir / filename(p.domain, p.url)
    p.path.write_text(text, encoding="utf-8")
    return p.path


@dataclass
class Skipped:
    url: str
    reason: str


def run(top: int = 5, out_dir: Path | None = None,
        fetcher: Callable[[str], Fetched | None] = fetch) -> tuple[list[Pitch], list[Skipped]]:
    out_dir = out_dir or settings.state_dir / "pitches"
    pitches, skipped = [], []
    for row in choose(top):
        got = fetcher(row["url"])
        if got is None or got.status != 200:
            skipped.append(Skipped(row["url"], f"could not read the page "
                                               f"({got.status if got else 'no response'})"))
            continue
        p = build(row, parse(got.html, got.final_url or row["url"]))
        write(p, out_dir)
        pitches.append(p)
    return pitches, skipped


# --- the Monday check ---------------------------------------------------------------------------

@dataclass
class Review:
    url: str
    listed_at: str
    verdict: str          # still | lost | unknown
    evidence: list[str]


def review(rows: list[dict] | None = None,
           fetcher: Callable[[str], Fetched | None] | None = None) -> list[Review]:
    """Re-read every `listed` page and say whether it still names or links DailyVox.

    Always a fresh fetch: the cache exists to stop re-reading pages that have not changed, and this
    is the one question whose whole point is that the page might have.
    """
    rows = outreach.stored("listed") if rows is None else rows
    fetcher = fetcher or (lambda u: fetch(u, max_age_days=0))
    out = []
    for r in rows:
        got = fetcher(r["url"])
        if got is None or got.status != 200:
            out.append(Review(r["url"], r.get("listed_at") or "", "unknown",
                              [f"could not read ({got.status if got else 'no response'})"]))
            continue
        ev = mentions_us(parse(got.html, got.final_url or r["url"]))
        out.append(Review(r["url"], r.get("listed_at") or "", "still" if ev else "lost", ev))
    return out
