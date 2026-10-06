"""Dead URLs that still carry value, and where each one should point instead.

A URL that returns 404 has not stopped existing for the rest of the web. Search Console goes on
reporting it for months — on 2026-10-06, 31 of the 176 pages with an impression in the last 90 days
were not in the URL inventory at all — and answer engines go on citing it: Gemini's grounding
results named getdailyvox.com 68 times between August and October, and every one of those is a
reader sent to whatever that URL serves today. When it serves a 404, the impression, the citation
and whatever link equity came with them land on nothing.

A 301 to the page that replaced it hands all three on. The risk is pointing it at the wrong page,
which is worse than a 404 — a reader who asked about anxiety journaling and lands on a travel
diary post has been misled, not helped. So the bar is set by the target choice, not the redirect:

    candidate URLs   every page with a Search Console impression in the window, every URL in the
                     inventory, and every getdailyvox.com URL an answer engine cited
    dead             the final response, after following redirects, is 404 or 410. 403, 429 and 5xx
                     are *unknown* — a rate limit or an outage says nothing about whether the page
                     exists, and redirecting a page that only blinked would replace it for good
    target           the live sitemap URL whose slug shares the most tokens, in the same section
                     (/blog/ to /blog/), scored 0..1. A slug that differs from a live one only by a
                     year suffix — `best-free-journal-app-2026` — is a rename and scores 0.95
    auto             confidence >= AUTO_CONFIDENCE *and* the dead URL earned an impression or a
                     citation. Value with no confident target, or a target with no value, is
                     reported and left for a person

Reads the open web (the liveness checks), so it runs in the composing half. It decides; it never
writes to the site. `publish/redirect.py` does that, from a ledger row, in the other environment.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger(__name__)

SITE = "https://getdailyvox.com"
HOSTS = {"getdailyvox.com", "www.getdailyvox.com"}

# A redirect ships with no human looking at it, so the bar is the one a person would want: a target
# so close that nobody would argue. Jaccard 0.8 is four of five slug tokens shared, in the same
# section, with no close runner-up.
AUTO_CONFIDENCE = 0.8

# A slug that differs from a live one only by a year is a rename, not a guess.
RENAME_CONFIDENCE = 0.95

# Same slug in a different section: almost certainly the same subject, but a /use/ page and a /blog/
# post are different kinds of page. Reported, never automatic.
CROSS_SECTION_EXACT = 0.75
CROSS_SECTION_FACTOR = 0.6

# When the runner-up is this close, the choice between them is a coin flip, and the confidence
# should say so.
AMBIGUITY_MARGIN = 0.1
AMBIGUITY_FACTOR = 0.85

DEAD = {404, 410}
TIMEOUT = 10.0
WORKERS = 4                     # polite: this is our own site, but it is also Vercel's edge
USER_AGENT = "autoseo (+https://github.com/intrepidkarthi/autoseo)"

# Tokens that carry no subject. "app" is deliberately not here: on this site it separates
# `voice-journal` (the practice) from `voice-journal-app` (the product comparison).
STOPWORDS = {"a", "an", "the", "for", "of", "to", "in", "on", "and", "or", "with", "your", "my",
             "how", "what", "is", "are", "do", "i", "you", "vs", "s"}
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_PAGINATION = re.compile(r"/page/\d+$")


# --- URLs -----------------------------------------------------------------------------------------

def normalise(url: str) -> str | None:
    """Canonical https://getdailyvox.com/path for one of our URLs, or None if it is not ours.

    Query strings and fragments go: GSC reports `?utm=` and `#faq` variants as separate pages, and
    they are all the same file on the site. A trailing slash goes too — the site serves
    `trailingSlash: false`, so `/blog/x/` and `/blog/x` are one page.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host not in HOSTS:
        return None
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return f"{SITE}{path}"


def path_of(url: str) -> str:
    return url.removeprefix(SITE) or "/"


def section(path: str) -> str:
    """First path segment: `blog` for /blog/x, "" for a root-level page like /about."""
    segs = [s for s in path.split("/") if s]
    return segs[0] if len(segs) > 1 else ""


def slug(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    return segs[-1] if segs else ""


def tokens(path: str) -> frozenset[str]:
    words = re.split(r"[-_.]+", slug(path).lower())
    return frozenset(w for w in words if w and w not in STOPWORDS and not _YEAR.match(w)
                     and not w.isdigit())


def _strip_year(s: str) -> str:
    return re.sub(r"-(?:19|20)\d\d(?=-|$)", "", s)


# --- target choice: pure ------------------------------------------------------------------------

@dataclass
class Suggestion:
    target: str            # a path
    confidence: float
    why: str


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _score(dead: str, live: str) -> tuple[float, str]:
    """How sure are we that `live` is what `dead` became? 0..1, with the reason."""
    same = section(dead) == section(live)
    d_slug, l_slug = slug(dead), slug(live)

    if same and d_slug != l_slug and _strip_year(d_slug) == _strip_year(l_slug):
        return RENAME_CONFIDENCE, "same slug without the year: a rename"
    if not same and d_slug == l_slug:
        return CROSS_SECTION_EXACT, f"same slug, different section (/{section(live)}/)"

    j = _jaccard(tokens(dead), tokens(live))
    shared = len(tokens(dead) & tokens(live))
    if same:
        return j, f"{shared} slug token(s) shared, Jaccard {j:.2f}"
    return j * CROSS_SECTION_FACTOR, (f"{shared} slug token(s) shared, Jaccard {j:.2f}, "
                                      f"but a different section")


def suggest(dead: str, live: Iterable[str]) -> Suggestion | None:
    """The best live replacement for a dead path, or None if nothing shares a single token.

    Never the homepage and never a pagination listing. A redirect to `/` is what Google calls a
    soft 404 and treats as one, and a /blog/page/3 listing changes contents every time a post ships.
    """
    scored: list[tuple[float, str, str]] = []
    for path in live:
        if path in ("/", dead) or _PAGINATION.search(path):
            continue
        conf, why = _score(dead, path)
        if conf > 0:
            scored.append((conf, path, why))
    if not scored:
        return None
    # Ties go to the shorter path, then alphabetically, so the same data always picks the same page.
    scored.sort(key=lambda s: (-s[0], len(s[1]), s[1]))
    best, target, why = scored[0]
    if len(scored) > 1 and best < RENAME_CONFIDENCE and scored[1][0] >= best - AMBIGUITY_MARGIN:
        best *= AMBIGUITY_FACTOR
        why += f"; runner-up {scored[1][1]} is close ({scored[1][0]:.2f})"
    return Suggestion(target=target, confidence=round(best, 3), why=why)


# --- liveness -----------------------------------------------------------------------------------

@dataclass
class Probe:
    url: str
    status: int | None                  # final status, None when the request itself failed
    chain: list[tuple[str, int]] = field(default_factory=list)   # redirect hops before the end
    error: str = ""

    @property
    def verdict(self) -> str:
        """dead | live | unknown. Only a definitive 404/410 is dead."""
        if self.status in DEAD:
            return "dead"
        if self.status is not None and 200 <= self.status < 300:
            return "live"
        return "unknown"


def probe(url: str, client: httpx.Client | None = None) -> Probe:
    """GET, following redirects, and record every hop on the way.

    GET rather than HEAD: Vercel answers HEAD correctly, but a 404 that only shows up on GET is
    exactly the case worth not missing, and these are our own pages — the bytes cost nothing.
    """
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                                    headers={"user-agent": USER_AGENT})
    try:
        r = client.get(url)
        chain = [(str(h.url), h.status_code) for h in r.history]
        return Probe(url=url, status=r.status_code, chain=chain)
    except Exception as exc:  # noqa: BLE001 — a failed request is unknown, never dead
        return Probe(url=url, status=None, error=type(exc).__name__)
    finally:
        if own:
            client.close()


def check(urls: Iterable[str], workers: int = WORKERS) -> dict[str, Probe]:
    urls = sorted(set(urls))
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                      headers={"user-agent": USER_AGENT}) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return dict(zip(urls, pool.map(lambda u: probe(u, client), urls), strict=True))


def serves_200(url: str) -> bool:
    """Is this a page in its own right, right now — no redirect, no error?

    A redirect target must pass this, because a target that itself redirects builds a chain and a
    target that 404s builds a redirect to nothing. Fail-closed: unreachable is not a target.
    """
    try:
        r = httpx.get(url, timeout=TIMEOUT, follow_redirects=False,
                      headers={"user-agent": USER_AGENT})
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def resolve_citation(url: str) -> str | None:
    """Where a Gemini grounding redirect points, if it points at us. One request, no follow.

    Not following is the point: following would land on our page and report *its* status as the
    redirect's, and the redirect URL is not the thing being cited. Old grounding redirects expire —
    the earliest stored ones return 404 from Google itself — and an expired redirect says nothing
    about our page, so it resolves to None rather than to "dead".
    """
    if "vertexaisearch" not in url:
        return normalise(url)
    try:
        r = httpx.head(url, timeout=TIMEOUT, follow_redirects=False,
                       headers={"user-agent": USER_AGENT})
    except Exception:  # noqa: BLE001
        return None
    if r.status_code not in (301, 302, 303, 307, 308):
        return None
    return normalise(r.headers.get("location", ""))


# --- evidence -----------------------------------------------------------------------------------

@dataclass
class Candidate:
    url: str
    impressions: float = 0.0
    clicks: float = 0.0
    citations: int = 0
    in_sitemap: bool = False
    sources: set[str] = field(default_factory=set)     # gsc | inventory | aeo

    @property
    def valuable(self) -> bool:
        """Something outside the site still points at it."""
        return self.impressions > 0 or self.citations > 0

    @property
    def value(self) -> float:
        """For ranking only. A click or a citation is a reader; an impression is a glance."""
        return self.impressions + 20 * self.clicks + 25 * self.citations + (5 if self.in_sitemap else 0)

    @property
    def evidence(self) -> str:
        bits = [f"{self.impressions:.0f} imp", f"{self.clicks:.0f} clk"]
        if self.citations:
            bits.append(f"cited {self.citations}x by AI")
        if self.in_sitemap:
            bits.append("was in sitemap")
        return ", ".join(bits)


def candidates(days: int = 90, resolver: Callable[[str], str | None] | None = None,
               inventory: bool = True) -> dict[str, Candidate]:
    """Every URL of ours that something still points at, with what points at it.

    `inventory=False` limits the set to URLs with outside value (impressions or citations), which
    is all the daily plan can act on: a dead URL nothing points at loses nothing by staying dead.
    """
    resolver = resolver or resolve_citation
    out: dict[str, Candidate] = {}

    def get(url: str) -> Candidate | None:
        norm = normalise(url)
        if norm is None:
            return None
        return out.setdefault(norm, Candidate(url=norm))

    with session() as conn:
        for r in conn.execute(
            """SELECT page, SUM(impressions) im, SUM(clicks) cl FROM gsc_page_daily
               WHERE date >= date('now', ?) GROUP BY page HAVING im > 0""",
            (f"-{days} days",),
        ):
            if c := get(r["page"]):
                c.impressions += r["im"] or 0
                c.clicks += r["cl"] or 0
                c.sources.add("gsc")

        sitemap = {normalise(r["url"]) for r in conn.execute(
            "SELECT url FROM url_inventory WHERE in_sitemap = 1")}
        if inventory:
            for r in conn.execute("SELECT url FROM url_inventory"):
                if c := get(r["url"]):
                    c.sources.add("inventory")

        cited = [r["url"] for r in conn.execute(
            "SELECT url FROM aeo_citation WHERE domain LIKE '%getdailyvox%' AND ts >= datetime('now', ?)",
            (f"-{days} days",),
        )]

    # Resolved once per distinct redirect, concurrently. 68 rows were 68 distinct URLs.
    distinct = sorted(set(cited))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        resolved = dict(zip(distinct, pool.map(resolver, distinct), strict=True))
    for raw in cited:
        if (target := resolved.get(raw)) and (c := get(target)):
            c.citations += 1
            c.sources.add("aeo")

    for url, c in out.items():
        c.in_sitemap = url in sitemap
    return out


def live_targets() -> set[str]:
    """Paths the sitemap says are pages. The pool a redirect may point into."""
    with session() as conn:
        return {path_of(n) for r in conn.execute("SELECT url FROM url_inventory WHERE in_sitemap = 1")
                if (n := normalise(r["url"]))}


# --- the finding --------------------------------------------------------------------------------

@dataclass
class Finding:
    candidate: Candidate
    probe: Probe
    suggestion: Suggestion | None
    action: str = "report"             # auto | report
    reason: str = ""

    @property
    def path(self) -> str:
        return path_of(self.candidate.url)


def gate(c: Candidate, s: Suggestion | None, already: set[str] = frozenset()) -> tuple[str, str]:
    """auto or report, and why. Pure; every refusal names its reason."""
    path = path_of(c.url)
    if path in already:
        return "report", "already redirected by an earlier run"
    if s is None:
        return "report", "no live page shares a slug token"
    if s.confidence < AUTO_CONFIDENCE:
        return "report", f"confidence {s.confidence:.2f} < {AUTO_CONFIDENCE}"
    if not c.valuable:
        return "report", "no impressions and no AI citations: nothing to reclaim"
    return "auto", s.why


@dataclass
class Result:
    findings: list[Finding]
    probes: dict[str, Probe]           # every candidate checked, dead or not

    @property
    def auto(self) -> list[Finding]:
        return [f for f in self.findings if f.action == "auto"]


def build(days: int = 90, valuable_only: bool = False, already: set[str] = frozenset(),
          checker: Callable[[Iterable[str]], dict[str, Probe]] | None = None,
          resolver: Callable[[str], str | None] | None = None,
          verify_target: Callable[[str], bool] | None = None) -> Result:
    """Every dead candidate, ranked by what it is still worth, with a suggested target each.

    `verify_target` is asked only about targets that would otherwise ship automatically — one GET
    per auto row, not one per page in the sitemap. A target that fails it is demoted to the report.
    """
    checker = checker or check
    verify_target = verify_target or (lambda p: serves_200(f"{SITE}{p}"))

    cands = candidates(days, resolver=resolver, inventory=not valuable_only)
    if valuable_only:
        cands = {u: c for u, c in cands.items() if c.valuable}
    probes = checker(cands)

    dead = {u for u, p in probes.items() if p.verdict == "dead"}
    pool = live_targets() - {path_of(u) for u in dead}

    findings: list[Finding] = []
    for url in dead:
        c = cands[url]
        s = suggest(path_of(url), pool)
        action, reason = gate(c, s, already)
        if action == "auto" and s and not verify_target(s.target):
            action, reason = "report", f"{s.target} does not serve a clean 200 right now"
        findings.append(Finding(candidate=c, probe=probes[url], suggestion=s,
                                action=action, reason=reason))

    findings.sort(key=lambda f: (-f.candidate.value, f.path))
    return Result(findings=findings, probes=probes)


def summarise(probes: dict[str, Probe]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in probes.values():
        out[p.verdict] = out.get(p.verdict, 0) + 1
    return out
