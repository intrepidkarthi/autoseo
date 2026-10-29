"""A technical audit of the live site, from its own sitemap. No paid API.

Added 2026-10-29. autoseo measured how Google *treats* the site (indexing, impressions, inspection
verdicts) and never looked at the pages themselves, so a declined page's verdict arrived with no
diagnosis attached. Thin content, duplicate titles, a canonical pointing somewhere else, a page no
other page links to — each is a classic reason for "Crawled - currently not indexed", and each is
checkable for free.

The rules are ported from open-seo (github.com/every-app/open-seo, MIT):
`src/server/lib/audit/issues/page-reporters.ts`, `multipage-checks.ts`, and the broken-link and
orphan checks in `features/audit/AuditScratchpad.ts`. Title and description bands are autoseo's
own, from `act.onpage`, so the audit and the retitler cannot disagree about what is too long.

Core Web Vitals come from Google's PageSpeed Insights API, which is free; open-seo buys the same
Lighthouse numbers from DataForSEO. A PageSpeed key is optional — unauthenticated calls work at the
volume this makes, a sample of the pages that earn impressions, once a week.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from defusedxml import ElementTree as DefusedET

from autoseo.core.config import settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

UA = "autoseo-audit (+https://github.com/intrepidkarthi/autoseo)"
WORKERS = 6
TIMEOUT = 20.0

THIN_WORDS = 150          # open-seo THIN_CONTENT_WORDS
SLOW_MS = 1500            # open-seo SLOW_RESPONSE_MS
TITLE_MIN = 10
DESC_MIN = 70
MAX_LINK_CHECKS = 400     # internal link targets outside the sitemap, probed per run

# Pages that are navigation rather than content. Crawled for their links — without them every
# post reachable only from /blog/page/4 reads as an orphan — but not audited themselves.
NAV_PATHS = ("/", "/blog", *(f"/blog/page/{n}" for n in range(2, 8)))

# The checks that bear on whether Google indexes a page. Title and description length are about
# the click once it ranks; listing them as reasons a page was declined would send the fixer after
# the wrong thing.
INDEXING = ("http-error", "redirect-in-sitemap", "noindex-in-sitemap", "canonical-elsewhere",
            "thin-content", "duplicate-title", "duplicate-description", "duplicate-content",
            "orphan-page", "slow-response", "missing-title")

SEVERITY = {
    "http-error": "error", "redirect-in-sitemap": "error", "noindex-in-sitemap": "error",
    "canonical-elsewhere": "error", "missing-title": "error", "redirect-loop": "error",
    "broken-internal-link": "error",
    "thin-content": "warning", "missing-h1": "warning", "multiple-h1": "warning",
    "duplicate-title": "warning", "duplicate-description": "warning", "duplicate-content": "warning",
    "title-length": "warning", "missing-description": "warning", "description-length": "warning",
    "slow-response": "warning", "orphan-page": "warning", "redirect-chain": "warning",
    "image-missing-alt": "notice", "heading-skip": "notice", "link-to-redirect": "notice",
}


# --- fetching and parsing ----------------------------------------------------------------------

@dataclass
class Page:
    url: str
    status: int = 0
    final_url: str = ""
    hops: list[str] = field(default_factory=list)
    ms: int = 0
    title: str = ""
    description: str = ""
    h1: int = 0
    headings: list[int] = field(default_factory=list)
    words: int = 0
    images_no_alt: int = 0
    canonical: str = ""
    noindex: bool = False
    links: set[str] = field(default_factory=set)
    content_hash: str = ""
    error: str = ""


class _Parser(HTMLParser):
    """Just enough of the page: head metadata, headings, body text, links, images."""

    _SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.description, self.canonical = "", "", ""
        self.noindex = False
        self.headings: list[int] = []
        self.links: list[str] = []
        self.images_no_alt = 0
        self._text: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                self.description = a.get("content", "").strip()
            elif name == "robots" and "noindex" in a.get("content", "").lower():
                self.noindex = True
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical = a.get("href", "").strip()
        elif re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img" and not a.get("alt", "").strip() and a.get("role") != "presentation":
            self.images_no_alt += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._text.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._text)).strip()


def _internal(href: str, base: str) -> str | None:
    """The site-relative path an href points at, or None if it leaves the site."""
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    absolute = urljoin(base, href)
    parsed = urlparse(absolute)
    if parsed.netloc and parsed.netloc.removeprefix("www.") != urlparse(settings.site).netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    if re.search(r"\.(png|jpe?g|gif|svg|webp|ico|css|js|xml|txt|pdf|json|mp4|mp3)$", path, re.I):
        return None
    return path


def parse(url: str, html: str) -> Page:
    p = _Parser()
    p.feed(html)
    words = len(p.text.split())
    return Page(
        url=url, title=p.title.strip(), description=p.description, h1=p.headings.count(1),
        headings=p.headings, words=words, images_no_alt=p.images_no_alt, canonical=p.canonical,
        noindex=p.noindex, links={x for x in (_internal(h, url) for h in p.links) if x},
        content_hash=hashlib.sha1(p.text.lower().encode()).hexdigest() if words >= THIN_WORDS else "",
    )


def fetch(client: httpx.Client, url: str) -> Page:
    started = time.monotonic()
    try:
        r = client.get(url)
    except Exception as exc:  # noqa: BLE001 — a dead fetch is an audit finding, not a crash
        return Page(url=url, error=str(exc)[:120])
    ms = int((time.monotonic() - started) * 1000)
    page = parse(url, r.text) if "html" in r.headers.get("content-type", "") else Page(url=url)
    page.status, page.final_url, page.ms = r.status_code, str(r.url), ms
    page.hops = [str(h.url) for h in r.history]
    if "noindex" in r.headers.get("x-robots-tag", "").lower():
        page.noindex = True
    return page


def sitemap_urls() -> list[str]:
    urls: list[str] = []
    queue, seen = [f"{settings.site}/sitemap.xml"], set()
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": UA}) as c:
        while queue:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                root = DefusedET.fromstring(c.get(sm).content)
            except Exception as exc:  # noqa: BLE001
                log.warning("audit: could not read %s: %s", sm, exc)
                continue
            for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                (queue if root.tag.endswith("sitemapindex") else urls).append(loc.text.strip())
    return sorted(set(urls))


# --- the checks ----------------------------------------------------------------------------------

def page_issues(p: Page) -> list[tuple[str, str]]:
    """Per-page findings for a sitemap URL. Ported from open-seo's `runPageReporters`."""
    from autoseo.act.onpage import DESC_MAX, TITLE_MAX

    if p.error:
        return [("http-error", p.error)]
    if p.status >= 400:
        return [("http-error", f"HTTP {p.status}")]
    out: list[tuple[str, str]] = []
    if p.hops:
        out.append(("redirect-in-sitemap", f"redirects to {p.final_url}"))
    if p.noindex:
        out.append(("noindex-in-sitemap", "listed in the sitemap but marked noindex"))
    if p.canonical:
        canonical = urljoin(p.final_url or p.url, p.canonical).rstrip("/")
        if canonical != (p.final_url or p.url).rstrip("/"):
            out.append(("canonical-elsewhere", f"canonical is {canonical}"))
    if not p.title:
        out.append(("missing-title", ""))
    elif not TITLE_MIN <= len(p.title) <= TITLE_MAX:
        out.append(("title-length", f"{len(p.title)} chars (want {TITLE_MIN}-{TITLE_MAX})"))
    if not p.description:
        out.append(("missing-description", ""))
    elif not DESC_MIN <= len(p.description) <= DESC_MAX:
        out.append(("description-length", f"{len(p.description)} chars (want {DESC_MIN}-{DESC_MAX})"))
    if p.h1 == 0:
        out.append(("missing-h1", ""))
    elif p.h1 > 1:
        out.append(("multiple-h1", f"{p.h1} H1s"))
    for prev, cur in zip(p.headings, p.headings[1:], strict=False):
        if cur > prev + 1:
            out.append(("heading-skip", f"h{prev} -> h{cur}"))
            break
    if p.words < THIN_WORDS:
        out.append(("thin-content", f"{p.words} words"))
    if p.images_no_alt:
        out.append(("image-missing-alt", f"{p.images_no_alt} image(s)"))
    if p.ms > SLOW_MS:
        out.append(("slow-response", f"{p.ms} ms"))
    return out


def site_issues(pages: list[Page], nav: list[Page],
                link_status: dict[str, tuple[int, str]]) -> list[tuple[str, str, str]]:
    """Findings that need the whole crawl: duplicates, chains, broken links, orphans."""
    out: list[tuple[str, str, str]] = []
    ok = [p for p in pages if not p.error and p.status < 400]

    for attr, check in (("title", "duplicate-title"), ("description", "duplicate-description"),
                        ("content_hash", "duplicate-content")):
        groups: dict[str, list[str]] = defaultdict(list)
        for p in ok:
            if value := getattr(p, attr).strip().lower():
                groups[value].append(p.url)
        for urls in groups.values():
            if len(urls) > 1:
                for u in urls:
                    others = ", ".join(x.replace(settings.site, "") for x in urls if x != u)
                    out.append((u, check, f"same as {others}"))

    for p in ok:
        if len(p.hops) > 1:
            out.append((p.url, "redirect-chain", f"{len(p.hops)} hops to {p.final_url}"))
        if p.final_url in p.hops:
            out.append((p.url, "redirect-loop", " -> ".join(p.hops)))

    # Broken links and links into a redirect, from the link graph of everything crawled.
    crawled = {urlparse(p.url).path.rstrip("/") or "/": p for p in [*ok, *nav]}
    for p in [*ok, *nav]:
        for path in sorted(p.links):
            if path in crawled:
                continue
            status, final = link_status.get(path, (0, ""))
            if status >= 400:
                out.append((p.url, "broken-internal-link", f"{path} returns {status}"))
            elif final and (urlparse(final).path.rstrip("/") or "/") != path:
                out.append((p.url, "link-to-redirect", f"{path} -> {urlparse(final).path}"))

    # Orphans: in the sitemap, linked from no other crawled page — the index pages included.
    inbound: dict[str, int] = defaultdict(int)
    for p in [*ok, *nav]:
        own = urlparse(p.url).path.rstrip("/") or "/"
        for path in p.links:
            if path != own:
                inbound[path] += 1
    for p in ok:
        path = urlparse(p.url).path.rstrip("/") or "/"
        if path != "/" and not inbound.get(path):
            out.append((p.url, "orphan-page", "no internal link points here"))
    return out


def _probe_links(paths: set[str]) -> dict[str, tuple[int, str]]:
    """HEAD every internal link target the crawl did not already fetch."""
    todo = sorted(paths)[:MAX_LINK_CHECKS]
    if len(paths) > MAX_LINK_CHECKS:
        log.warning("audit: %d link targets, probing the first %d", len(paths), MAX_LINK_CHECKS)

    def head(path: str) -> tuple[str, tuple[int, str]]:
        try:
            r = httpx.head(settings.site + path, timeout=TIMEOUT, follow_redirects=True,
                           headers={"user-agent": UA})
            if r.status_code == 405:
                r = httpx.get(settings.site + path, timeout=TIMEOUT, follow_redirects=True,
                              headers={"user-agent": UA})
            return path, (r.status_code, str(r.url))
        except Exception:  # noqa: BLE001 — unreachable is unknown, never "broken"
            return path, (0, "")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return dict(pool.map(head, todo))


# --- Core Web Vitals -----------------------------------------------------------------------------

PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


class PageSpeedRefused(RuntimeError):
    """429 or 403: no key, or the key's quota is gone. Every later call would get the same."""


def pagespeed(url: str) -> dict | None:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if key := settings.pagespeed_api_key:
        params["key"] = key
    try:
        r = httpx.get(PSI, params=params, timeout=90.0)
        if r.status_code in (403, 429):
            raise PageSpeedRefused(f"PageSpeed returned {r.status_code} — set PAGESPEED_API_KEY")
        r.raise_for_status()
        data = r.json()
    except PageSpeedRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("pagespeed failed for %s: %s", url, str(exc)[:100])
        return None
    audits = data.get("lighthouseResult", {}).get("audits", {})
    field_data = data.get("loadingExperience", {}).get("metrics", {})

    def lab(name: str) -> float | None:
        return audits.get(name, {}).get("numericValue")

    def real(name: str) -> float | None:
        return field_data.get(name, {}).get("percentile")

    perf = data.get("lighthouseResult", {}).get("categories", {}).get("performance", {}).get("score")
    cls_field = real("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    return {
        "perf": round(perf * 100) if perf is not None else None,
        "lcp_ms": lab("largest-contentful-paint"), "cls": lab("cumulative-layout-shift"),
        "tbt_ms": lab("total-blocking-time"),
        "field_lcp_ms": real("LARGEST_CONTENTFUL_PAINT_MS"),
        "field_cls": cls_field / 100 if cls_field is not None else None,
        "field_inp_ms": real("INTERACTION_TO_NEXT_PAINT"),
    }


def _top_pages(n: int) -> list[str]:
    start = (dt.date.today() - dt.timedelta(days=28)).isoformat()
    with session() as conn:
        return [r["page"] for r in conn.execute(
            "SELECT page, SUM(impressions) imp FROM gsc_page_daily WHERE date >= ? "
            "GROUP BY page ORDER BY imp DESC LIMIT ?", (start, n))]


# --- the run -------------------------------------------------------------------------------------

@dataclass
class Result:
    pages: int
    issues: list[tuple[str, str, str, str]]   # (url, check, severity, detail)
    cwv: int = 0

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = defaultdict(int)
        for _, check, _, _ in self.issues:
            c[check] += 1
        return dict(c)


def run(psi: int = 0) -> Result:
    urls = sitemap_urls()
    if not urls:
        log.warning("audit: the sitemap returned no URLs — nothing audited")
        return Result(0, [])
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers={"user-agent": UA}) as c:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            pages = list(pool.map(lambda u: fetch(c, u), urls))
            nav = list(pool.map(lambda p: fetch(c, settings.site + p), NAV_PATHS))
    nav = [p for p in nav if not p.error and p.status < 400]

    known = {urlparse(p.url).path.rstrip("/") or "/" for p in [*pages, *nav]}
    targets = {path for p in [*pages, *nav] for path in p.links} - known
    link_status = _probe_links(targets)

    issues = [(p.url, check, SEVERITY.get(check, "notice"), detail)
              for p in pages for check, detail in page_issues(p)]
    issues += [(u, check, SEVERITY.get(check, "notice"), detail)
               for u, check, detail in site_issues(pages, nav, link_status)]
    result = Result(len(pages), issues)

    today = dt.date.today().isoformat()
    with session() as conn:
        # The issue table holds the latest audit only; the summary keeps the trend.
        conn.execute("DELETE FROM audit_issue")
        conn.executemany(
            "INSERT OR IGNORE INTO audit_issue(date, url, check_name, severity, detail) "
            "VALUES (?,?,?,?,?)", [(today, *i) for i in issues])
        conn.execute("DELETE FROM audit_summary WHERE date = ?", (today,))
        conn.executemany("INSERT INTO audit_summary(date, check_name, n) VALUES (?,?,?)",
                         [(today, k, n) for k, n in result.counts().items()])
        conn.execute("INSERT OR REPLACE INTO audit_summary(date, check_name, n) VALUES (?,?,?)",
                     (today, "_pages", len(pages)))

    for url in _top_pages(psi) if psi else []:
        try:
            m = pagespeed(url)
        except PageSpeedRefused as exc:
            log.warning("audit: %s; Core Web Vitals skipped this run", exc)
            break
        if m:
            with session() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO audit_cwv(date, url, perf, lcp_ms, cls, tbt_ms, "
                    "field_lcp_ms, field_cls, field_inp_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                    (today, url, m["perf"], m["lcp_ms"], m["cls"], m["tbt_ms"],
                     m["field_lcp_ms"], m["field_cls"], m["field_inp_ms"]))
            result.cwv += 1
    log.info("audit: %d pages, %d issues, %d Core Web Vitals readings",
             result.pages, len(issues), result.cwv)
    return result


def describe(top: int = 6) -> str:
    """The audit as the brief prints it: counts with their change, then what touches unindexed pages."""
    with session() as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM audit_summary ORDER BY date DESC LIMIT 2")]
        if not dates:
            return "  No audit yet — `autoseo audit` crawls the sitemap."
        now = {r["check_name"]: r["n"] for r in conn.execute(
            "SELECT check_name, n FROM audit_summary WHERE date = ?", (dates[0],))}
        before = {r["check_name"]: r["n"] for r in conn.execute(
            "SELECT check_name, n FROM audit_summary WHERE date = ?", (dates[-1],))}
        declined = conn.execute(
            f"""SELECT a.url, GROUP_CONCAT(a.check_name || CASE WHEN a.detail != '' THEN ' (' ||
                      a.detail || ')' ELSE '' END, '; ') issues
               FROM audit_issue a JOIN url_index_status s ON s.url = a.url
               WHERE s.indexed = 0 AND a.check_name IN ({",".join("?" * len(INDEXING))})
               GROUP BY a.url ORDER BY a.url LIMIT ?""", (*INDEXING, top * 3)).fetchall()
        cwv = conn.execute(
            """SELECT url, perf, lcp_ms, cls, field_lcp_ms, field_inp_ms FROM audit_cwv
               WHERE date = (SELECT MAX(date) FROM audit_cwv) ORDER BY perf LIMIT ?""",
            (top,)).fetchall()

    lines = [f"  {now.pop('_pages', 0)} sitemap pages audited {dates[0]}"]
    before.pop("_pages", None)
    order = sorted(now, key=lambda k: ({"error": 0, "warning": 1}.get(SEVERITY.get(k), 2), -now[k]))
    for k in order:
        delta = now[k] - before.get(k, 0) if len(dates) > 1 else 0
        change = f" ({delta:+d})" if delta else ""
        lines.append(f"  {SEVERITY.get(k, 'notice'):<8}{k:<24}{now[k]:>5}{change}")
    if declined:
        lines.append("\n  on pages Google has not indexed, the findings that bear on indexing:")
        for r in declined:
            lines.append(f"      {r['url'].replace(settings.site, '')}: {r['issues'][:160]}")
    if cwv:
        lines.append("\n  Core Web Vitals, mobile, slowest of the top pages:")
        for r in cwv:
            field = (f", field LCP {r['field_lcp_ms']:.0f} ms INP {r['field_inp_ms']:.0f} ms"
                     if r["field_lcp_ms"] and r["field_inp_ms"] else "")
            lines.append(f"      perf {r['perf']}  LCP {r['lcp_ms'] / 1000:.1f}s  "
                         f"CLS {r['cls']:.2f}{field}  {r['url'].replace(settings.site, '')}")
    return "\n".join(lines)


def noindexed() -> set[str]:
    """Sitemap URLs the latest crawl found marked noindex — whatever Search Console still says.

    Google's verdict is only as fresh as its last crawl. Five pages noindexed on 12 Aug still read
    "Submitted and indexed" on 28 Oct, last fetched in early August, and the fold arm redirected a
    page into one of them on 22 Oct. The page's own robots tag is the current fact.
    """
    with session() as conn:
        return {r[0] for r in conn.execute(
            "SELECT url FROM audit_issue WHERE check_name = 'noindex-in-sitemap'")}
