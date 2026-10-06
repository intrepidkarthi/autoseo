"""Pitches: read the roundup, find the opening, draft the email, never send it.

Mostly about being right before being useful: a timeout is not a dead link, a pitch never claims a
fact outside the fixed block, and every draft clears the same write-like-me gate the articles do.
"""
from __future__ import annotations

import datetime as dt
import socket

import httpx
import pytest

from autoseo.decide import pitch

# --- reading a page -----------------------------------------------------------------------------

ROUNDUP = """<html><head><title>Best Voice Journal Apps — 2026</title>
<meta name="description" content="We tested nine voice journaling apps for privacy and price.">
<script>var DailyVox = "not visible text";</script></head><body>
<h1>Best Voice Journal Apps</h1>
<h2>1. Day One</h2><p>Day One is the default. <a href="https://dayoneapp.com/">Get Day One</a></p>
<h2>2. Rosebud</h2><p>An AI journal. <a href="https://rosebud.app/old-page">Rosebud</a></p>
<h2>3. Otter.ai</h2><p>Transcribes meetings, works for journaling too.</p>
<p>Journey through your week with a prompt.</p>
<a href="/about">About us</a>
</body></html>"""


def page(html: str = ROUNDUP, url: str = "https://example.com/best-voice-journal-apps") -> pitch.Page:
    return pitch.parse(html, url)


def test_parse_reads_title_description_headings_and_links():
    p = page()
    assert p.title.startswith("Best Voice Journal Apps")
    assert p.description.startswith("We tested nine")
    assert "2. Rosebud" in p.headings
    assert ("https://example.com/about", "About us") in p.links
    assert "not visible text" not in p.text


@pytest.mark.parametrize("snippet, evidence", [
    ("<p>We also like DailyVox.</p>", "names DailyVox"),
    ("<p>Daily Vox is new.</p>", "names DailyVox"),
    ('<a href="https://getdailyvox.com/">site</a>', "links getdailyvox.com"),
    ('<a href="https://apps.apple.com/us/app/dailyvox/id6760454642">get</a>', "links id6760454642"),
    ('<a href="https://play.google.com/store/apps/details?id=com.dailyvox.app">get</a>',
     "links com.dailyvox.app"),
])
def test_mention_detection(snippet, evidence):
    assert evidence in pitch.mentions_us(page(ROUNDUP.replace("</body>", snippet + "</body>")))


def test_no_mention():
    assert pitch.mentions_us(page()) == []


def test_competitors_and_their_links():
    named = pitch.competitors_named(page())
    assert named["Day One"] == ["https://dayoneapp.com/"]
    assert named["Rosebud"] == ["https://rosebud.app/old-page"]
    assert "Otter" in named                 # ambiguous, but written as its domain
    assert "Journey" not in named           # "Journey through your week" is a sentence, not an app


def test_a_vendor_blog_does_not_list_itself():
    p = page(url="https://www.rosebud.app/blog/best-journaling-apps")
    assert "Rosebud" not in pitch.competitors_named(p)
    assert pitch.vendor_of("rosebud.app") == "Rosebud"


# --- dead links ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status, final, body, verdict", [
    (404, "", "", "dead"),
    (410, "", "", "dead"),
    (200, "https://rosebud.app/", "<h1>Rosebud</h1>", "live"),
    (200, "https://x.app/", "This domain is for sale! Buy this domain today.", "dead"),
    (200, "https://www.sedo.com/search/details/?domain=x.app", "", "dead"),
    (403, "", "", "unknown"),
    (429, "", "", "unknown"),
    (500, "", "", "unknown"),
    (503, "", "", "unknown"),
])
def test_classify(status, final, body, verdict):
    assert pitch.classify(status, final, body)[0] == verdict


def _client(exc: Exception) -> httpx.Client:
    def handler(request):
        raise exc
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_timeout_is_unknown_never_dead():
    chk = pitch.check_link("https://slow.app/", _client(httpx.ReadTimeout("slow")),
                           nxdomain=lambda h: True)
    assert chk.verdict == "unknown"


def test_a_domain_that_does_not_resolve_is_dead():
    chk = pitch.check_link("https://gone.app/", _client(httpx.ConnectError("dns")),
                           nxdomain=lambda h: True)
    assert chk.verdict == "dead" and "resolve" in chk.detail


def test_a_connect_error_on_a_resolving_domain_is_unknown():
    chk = pitch.check_link("https://flaky.app/", _client(httpx.ConnectError("refused")),
                           nxdomain=lambda h: False)
    assert chk.verdict == "unknown"


def test_a_temporary_resolver_failure_is_not_nxdomain(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert pitch._nxdomain("anything.app") is False


def test_live_links_on_a_page_produce_no_dead_entry():
    row = {"url": "https://example.com/best", "domain": "example.com", "citations": 3}
    p = pitch.build(row, page(), link_checker=lambda urls: {
        u: pitch.LinkCheck(u, "dead" if "old-page" in u else "live", "HTTP 404") for u in urls})
    assert [(n, c.url) for n, c in p.dead] == [("Rosebud", "https://rosebud.app/old-page")]


# --- the cache ----------------------------------------------------------------------------------

def _getter(calls: list, status: int = 200):
    def get(url):
        calls.append(url)
        return pitch.Fetched(url=url, status=status, final_url=url, html="<title>x</title>",
                             fetched_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    return get


def test_fetch_caches_for_a_week(tmp_path):
    calls: list = []
    for _ in range(3):
        assert pitch.fetch("https://e.com/a", cache_dir=tmp_path, getter=_getter(calls)).status == 200
    assert len(calls) == 1
    pitch.fetch("https://e.com/a", max_age_days=0, cache_dir=tmp_path, getter=_getter(calls))
    assert len(calls) == 2                  # the review path always refetches


def test_fetch_never_caches_a_failure(tmp_path):
    calls: list = []
    for _ in range(2):
        pitch.fetch("https://e.com/a", cache_dir=tmp_path, getter=_getter(calls, status=403))
    assert len(calls) == 2


# --- the draft ----------------------------------------------------------------------------------

def _pitch(**kw) -> pitch.Pitch:
    base = dict(url="https://example.com/best-voice-journal-apps-2026/", domain="example.com",
                title="Best Voice Journal Apps — 2026 Edition 🎙",
                covers="We tested nine voice journaling apps for privacy and price — honestly.",
                citations=6, mentioned=[], competitors={"Rosebud": ["https://rosebud.app/x"],
                                                        "Day One": []},
                dead=[("Rosebud", pitch.LinkCheck("https://rosebud.app/x", "dead", "HTTP 404"))],
                unknown=2)
    return pitch.Pitch(**{**base, **kw})


@pytest.mark.parametrize("variant", [
    {},
    {"dead": [], "unknown": 0},
    {"mentioned": ["names DailyVox"], "citations": 0},
    {"dead": [("Rosebud", pitch.LinkCheck("https://gone.app", "dead", "domain does not resolve"))]},
    {"domain": "lound.ai", "competitors": {}, "dead": []},
])
def test_every_draft_passes_the_gate(variant):
    text = pitch.render(_pitch(**variant))
    _, verdict = pitch.gated(text)
    assert verdict.startswith("PASS"), verdict
    assert text.splitlines()[0] == pitch.HEADER
    assert "—" not in text and "–" not in text
    assert "🎙" not in text


def test_the_fact_block_is_the_only_source_of_claims():
    text = pitch.render(_pitch())
    assert pitch.FACTS in text and pitch.BLURB in text
    for link in ("https://getdailyvox.com", "https://apps.apple.com/app/id6760454642",
                 "https://play.google.com/store/apps/details?id=com.dailyvox.app"):
        assert link in text


def test_render_is_deterministic():
    assert pitch.render(_pitch()) == pitch.render(_pitch())


def test_a_vendor_blog_is_flagged():
    assert "own blog" in pitch.render(_pitch(domain="rosebud.app"))


def test_filename():
    assert pitch.filename("lound.ai", "https://lound.ai/blog/best-voice-journal-app-2026/") == \
        "lound.ai-best-voice-journal-app-2026.md"
    assert pitch.filename("example.com", "https://example.com/") == "example.com-home.md"


def test_write_puts_the_draft_where_asked(tmp_path):
    p = _pitch()
    path = pitch.write(p, tmp_path)
    assert path.read_text(encoding="utf-8").startswith(pitch.HEADER)
    assert p.gate.startswith("PASS")


# --- choosing targets ---------------------------------------------------------------------------

def _row(url, domain, state="new", **kw):
    return {"url": url, "domain": domain, "title": domain, "state": state, "citations": 2, **kw}


def test_choose_skips_stores_forums_expired_redirects_and_non_roundups():
    rows = [
        _row("https://apps.apple.com/us/app/diarium/id1436044299", "apple.com"),
        _row("https://www.reddit.com/r/x/comments/1/best_ai_journaling_app/", "reddit.com"),
        _row("https://vertexaisearch.cloud.google.com/grounding-api-redirect/expired", "foo.com"),
        _row("https://journallm.app/", "journallm.app"),                       # not a roundup
        _row("https://www.rosebud.app/blog/best-journaling-apps", "rosebud.app"),
        _row("https://vertexaisearch.cloud.google.com/grounding-api-redirect/ok", "holstee.com"),
    ]

    def resolver(url):
        return ("https://www.holstee.com/blogs/best-journaling-apps" if url.endswith("/ok") else url)

    chosen = pitch.choose(5, rows=rows, resolver=resolver)
    # Independent first, the vendor's own blog last.
    assert [r["url"] for r in chosen] == ["https://www.holstee.com/blogs/best-journaling-apps",
                                          "https://www.rosebud.app/blog/best-journaling-apps"]


def test_choose_stops_at_top():
    rows = [_row(f"https://e{i}.com/best-apps", f"e{i}.com") for i in range(10)]
    assert len(pitch.choose(3, rows=rows, resolver=lambda u: u)) == 3


# --- the Monday check ---------------------------------------------------------------------------

def test_review_reports_lost_listings():
    rows = [_row("https://a.com/best", "a.com", state="listed", listed_at="2026-09-01T00:00:00"),
            _row("https://b.com/best", "b.com", state="listed", listed_at="2026-09-02T00:00:00"),
            _row("https://c.com/best", "c.com", state="listed", listed_at="2026-09-03T00:00:00")]
    pages = {
        "https://a.com/best": "<p>We like DailyVox.</p>",
        "https://b.com/best": "<p>Day One and Rosebud.</p>",
    }

    def fetcher(url):
        if url not in pages:
            return None
        return pitch.Fetched(url=url, status=200, final_url=url, html=pages[url], fetched_at="")

    got = {r.url: r.verdict for r in pitch.review(rows=rows, fetcher=fetcher)}
    assert got == {"https://a.com/best": "still", "https://b.com/best": "lost",
                   "https://c.com/best": "unknown"}
