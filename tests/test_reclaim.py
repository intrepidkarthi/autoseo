"""Reclaiming dead URLs: which are dead, what they are worth, and where they should point.

A redirect ships with nobody looking, and the wrong target is worse than the 404 it replaces. So
most of these tests are about refusing: a 403 is not dead, a guess is not a rename, a URL nothing
points at is not worth a redirect, and two per run is the most the plan may queue.
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from autoseo.decide import reclaim

SITE = "https://getdailyvox.com"
RECENT = (dt.date.today() - dt.timedelta(days=5)).isoformat()


# --- URLs and tokens ----------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("https://getdailyvox.com/blog/x", f"{SITE}/blog/x"),
    ("https://getdailyvox.com/blog/x/", f"{SITE}/blog/x"),
    ("https://www.getdailyvox.com/blog/x?utm_source=a#faq", f"{SITE}/blog/x"),
    ("https://getdailyvox.com", f"{SITE}/"),
    ("https://getdailyvox.com/", f"{SITE}/"),
    ("https://example.com/blog/x", None),
    ("https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", None),
])
def test_normalise(raw, want):
    assert reclaim.normalise(raw) == want


def test_tokens_drop_years_and_filler():
    assert reclaim.tokens("/blog/best-free-journal-app-2026") == {"best", "free", "journal", "app"}
    assert reclaim.tokens("/blog/how-to-journal-for-anxiety") == {"journal", "anxiety"}


def test_section():
    assert reclaim.section("/blog/x") == "blog"
    assert reclaim.section("/about") == ""
    assert reclaim.section("/") == ""


# --- target choice ------------------------------------------------------------------------------

LIVE = {
    "/", "/about", "/blog", "/blog/page/2",
    "/blog/best-free-journal-app",
    "/blog/best-journal-app-for-anxiety",
    "/blog/travel-journal-app",
    "/use/voice-journal",
}


def test_year_suffix_is_a_rename():
    s = reclaim.suggest("/blog/best-free-journal-app-2026", LIVE)
    assert s.target == "/blog/best-free-journal-app"
    assert s.confidence == reclaim.RENAME_CONFIDENCE


def test_same_section_beats_a_cross_section_match():
    s = reclaim.suggest("/blog/journal-app-for-anxiety", LIVE)
    assert s.target == "/blog/best-journal-app-for-anxiety"
    assert reclaim.section(s.target) == "blog"


def test_cross_section_exact_slug_is_reported_not_automatic():
    s = reclaim.suggest("/blog/voice-journal", LIVE - {"/blog/best-free-journal-app"})
    assert s.target == "/use/voice-journal"
    assert s.confidence < reclaim.AUTO_CONFIDENCE


def test_never_the_homepage_or_a_pagination_listing():
    s = reclaim.suggest("/blog/page", {"/", "/blog/page/2"})
    assert s is None


def test_nothing_shared_means_no_suggestion():
    assert reclaim.suggest("/blog/zebra-crossing", LIVE) is None


def test_a_close_runner_up_lowers_confidence():
    """Two pages equally like the dead one: the choice is a coin flip and the score must say so."""
    live = {"/blog/voice-diary-app-iphone", "/blog/voice-diary-app-android"}
    s = reclaim.suggest("/blog/voice-diary-app", live)
    assert s.confidence < reclaim.AUTO_CONFIDENCE
    assert "runner-up" in s.why


def test_target_choice_is_deterministic():
    live = {"/blog/b-voice-journal", "/blog/a-voice-journal"}
    picks = {reclaim.suggest("/blog/voice-journal-x", live).target for _ in range(5)}
    assert picks == {"/blog/a-voice-journal"}


# --- liveness -----------------------------------------------------------------------------------

@pytest.mark.parametrize("status, verdict", [
    (200, "live"), (404, "dead"), (410, "dead"),
    (403, "unknown"), (429, "unknown"), (500, "unknown"), (503, "unknown"), (None, "unknown"),
])
def test_only_404_and_410_are_dead(status, verdict):
    assert reclaim.Probe(url="u", status=status).verdict == verdict


def _client(routes: dict[str, httpx.Response | Exception]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        out = routes[str(request.url)]
        if isinstance(out, Exception):
            raise out
        return out
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_a_redirect_chain_that_ends_in_404_is_dead_and_recorded():
    client = _client({
        f"{SITE}/blog/old": httpx.Response(301, headers={"location": f"{SITE}/blog/newer"}),
        f"{SITE}/blog/newer": httpx.Response(404),
    })
    p = reclaim.probe(f"{SITE}/blog/old", client)
    assert p.verdict == "dead"
    assert p.chain == [(f"{SITE}/blog/old", 301)]


def test_a_redirect_to_a_live_page_is_live():
    client = _client({
        f"{SITE}/blog/old": httpx.Response(308, headers={"location": f"{SITE}/blog/new"}),
        f"{SITE}/blog/new": httpx.Response(200),
    })
    assert reclaim.probe(f"{SITE}/blog/old", client).verdict == "live"


def test_a_timeout_is_unknown_never_dead():
    client = _client({f"{SITE}/blog/x": httpx.ReadTimeout("slow")})
    p = reclaim.probe(f"{SITE}/blog/x", client)
    assert p.verdict == "unknown"
    assert p.error == "ReadTimeout"


# --- the gate -----------------------------------------------------------------------------------

def _cand(path="/blog/x", imp=10.0, cites=0):
    return reclaim.Candidate(url=f"{SITE}{path}", impressions=imp, citations=cites)


def test_gate_auto_needs_confidence_and_value():
    good = reclaim.Suggestion("/blog/y", 0.95, "rename")
    assert reclaim.gate(_cand(), good)[0] == "auto"
    assert reclaim.gate(_cand(imp=0, cites=2), good)[0] == "auto"      # citations alone count


def test_gate_reports_below_threshold():
    weak = reclaim.Suggestion("/blog/y", reclaim.AUTO_CONFIDENCE - 0.01, "guess")
    action, why = reclaim.gate(_cand(), weak)
    assert action == "report" and "confidence" in why


def test_gate_reports_a_url_nothing_points_at():
    good = reclaim.Suggestion("/blog/y", 0.95, "rename")
    action, why = reclaim.gate(_cand(imp=0, cites=0), good)
    assert action == "report" and "nothing to reclaim" in why


def test_gate_never_redirects_twice():
    good = reclaim.Suggestion("/blog/y", 0.95, "rename")
    assert reclaim.gate(_cand(), good, already={"/blog/x"})[0] == "report"


def test_gate_with_no_target():
    assert reclaim.gate(_cand(), None)[0] == "report"


# --- build, end to end over the database --------------------------------------------------------

def _seed(conn):
    rows = [
        ("/blog/best-free-journal-app-2026", 40, 1),     # dead, renamed, valuable -> auto
        ("/blog/zebra-crossing", 12, 0),                 # dead, valuable, no target -> report
        ("/blog/best-free-journal-app", 300, 9),         # live
    ]
    for path, imp, clk in rows:
        conn.execute("INSERT INTO gsc_page_daily (date, page, clicks, impressions, position) "
                     "VALUES (?,?,?,?,5)", (RECENT, f"{SITE}{path}", clk, imp))
    for path, in_sm in (("/blog/best-free-journal-app", 1), ("/blog/old-and-unloved", 0),
                        ("/blog/best-journal-app-for-anxiety", 1)):
        conn.execute("INSERT INTO url_inventory (url, cluster, in_sitemap, first_seen) "
                     "VALUES (?, 'blog', ?, ?)", (f"{SITE}{path}", in_sm, RECENT))
    conn.execute("INSERT INTO aeo_citation (ts, question_id, url, domain, title) VALUES "
                 "(datetime('now'), 'q1', 'https://vertexaisearch.cloud.google.com/r/1', "
                 "'getdailyvox.com', 'getdailyvox.com')")
    conn.commit()


DEAD_PATHS = {"/blog/best-free-journal-app-2026", "/blog/zebra-crossing", "/blog/journal-app-anxiety",
              "/blog/old-and-unloved"}


def _checker(urls):
    return {u: reclaim.Probe(url=u, status=404 if reclaim.path_of(u) in DEAD_PATHS else 200)
            for u in urls}


def _resolver(url):
    return f"{SITE}/blog/journal-app-anxiety"          # an AI citation of a dead page


def test_build_ranks_and_gates(db):
    from autoseo.core.db import session
    with session() as conn:
        _seed(conn)

    result = reclaim.build(checker=_checker, resolver=_resolver, verify_target=lambda p: True)
    by_path = {f.path: f for f in result.findings}

    assert set(by_path) == DEAD_PATHS
    assert by_path["/blog/best-free-journal-app-2026"].action == "auto"
    assert by_path["/blog/zebra-crossing"].action == "report"
    # Inventory-only, no impressions and no citations: worth reporting, never worth a redirect.
    assert by_path["/blog/old-and-unloved"].action == "report"
    assert by_path["/blog/journal-app-anxiety"].candidate.citations == 1
    # Ranked by value: 40 impressions and a click outrank one citation and twelve impressions.
    assert result.findings[0].path == "/blog/best-free-journal-app-2026"


def test_build_valuable_only_skips_inventory_only_urls(db):
    from autoseo.core.db import session
    with session() as conn:
        _seed(conn)
    result = reclaim.build(valuable_only=True, checker=_checker, resolver=_resolver,
                           verify_target=lambda p: True)
    assert "/blog/old-and-unloved" not in {f.path for f in result.findings}


def test_a_target_that_does_not_serve_200_is_demoted(db):
    from autoseo.core.db import session
    with session() as conn:
        _seed(conn)
    result = reclaim.build(checker=_checker, resolver=_resolver, verify_target=lambda p: False)
    assert not result.auto


def test_an_expired_grounding_redirect_counts_for_nothing(db):
    from autoseo.core.db import session
    with session() as conn:
        _seed(conn)
    result = reclaim.build(checker=_checker, resolver=lambda u: None, verify_target=lambda p: True)
    assert "/blog/journal-app-anxiety" not in {f.path for f in result.findings}


# --- the planner and its cap --------------------------------------------------------------------

def _finding(n: int) -> reclaim.Finding:
    c = reclaim.Candidate(url=f"{SITE}/blog/dead-{n}", impressions=10)
    return reclaim.Finding(candidate=c, probe=reclaim.Probe(url=c.url, status=404),
                           suggestion=reclaim.Suggestion(f"/blog/live-{n}", 0.95, "rename"),
                           action="auto")


def test_plan_queues_at_most_two_per_run(db, monkeypatch):
    from autoseo.act import ledger, plan, policy

    monkeypatch.setattr(reclaim, "build", lambda *a, **k: reclaim.Result(
        findings=[_finding(i) for i in range(5)], probes={}))
    result = plan.Planned()
    plan._plan_reclaim(90, result, dry_run=False)

    queued = ledger.planned(ledger.Kind.REDIRECT)
    assert len(queued) == policy.MAX_REDIRECTS_PER_RUN == 2
    assert queued[0].meta["source"] == "/blog/dead-0"
    assert queued[0].meta["destination"] == "/blog/live-0"

    # The next run, with those two still waiting, plans nothing more.
    again = plan.Planned()
    plan._plan_reclaim(90, again, dry_run=False)
    assert len(ledger.planned(ledger.Kind.REDIRECT)) == 2
    assert again.redirected == 0


def test_plan_dry_run_queues_nothing(db, monkeypatch):
    from autoseo.act import ledger, plan

    monkeypatch.setattr(reclaim, "build", lambda *a, **k: reclaim.Result(
        findings=[_finding(1)], probes={}))
    result = plan.Planned()
    plan._plan_reclaim(90, result, dry_run=True)
    assert result.redirected == 1
    assert not ledger.planned(ledger.Kind.REDIRECT)


def test_a_shipped_reclaim_is_never_proposed_again(db):
    from autoseo.act import ledger, policy

    item_id = ledger.plan(ledger.Item(kind=ledger.Kind.REDIRECT, title="301", body="", rationale="r",
                                      meta={"source": "/blog/dead-1", "destination": "/blog/x"}))
    ledger.ship(item_id, "https://github.com/x/commit/1")
    assert "/blog/dead-1" in policy.already_redirected()


# --- apply --------------------------------------------------------------------------------------

@pytest.fixture
def applier(db, monkeypatch):
    """`apply.run` with every network edge stubbed and the calls recorded."""
    from dataclasses import replace

    from autoseo.act import apply
    from autoseo.core import config
    from autoseo.publish import blog, delist, indexnow, redirect, sitemap

    calls: list[tuple] = []
    monkeypatch.setattr(config, "settings", replace(config.settings, gh_dailyvox_token="t"))
    for mod, name in ((delist, "apply"), (blog, "relink"), (indexnow, "ensure_key")):
        monkeypatch.setattr(mod, name, lambda dry_run=False: "")
    monkeypatch.setattr(indexnow, "submit", lambda urls, dry_run=False: len(urls))
    monkeypatch.setattr(sitemap, "drop_urls",
                        lambda urls, why, dry_run=False: calls.append(("drop", urls)) or "")
    monkeypatch.setattr(redirect, "add", lambda s, d, why, dry_run=False, strict=False:
                        calls.append(("add", s, d, strict)) or "https://github.com/c/1")
    return apply, calls


def _queue_redirect(created: str | None = None) -> int:
    from autoseo.act import ledger
    from autoseo.core.db import session

    item_id = ledger.plan(ledger.Item(
        kind=ledger.Kind.REDIRECT, title="301 /blog/dead -> /blog/live", body="", rationale="r",
        meta={"source": "/blog/dead", "destination": "/blog/live", "url": f"{SITE}/blog/dead"}))
    if created:
        with session() as conn:
            conn.execute("UPDATE queue_item SET created = ? WHERE id = ?", (created, item_id))
    return item_id


def test_apply_ships_a_reclaim_strictly_and_drops_it_from_the_sitemap(applier):
    from autoseo.act import ledger

    apply, calls = applier
    item_id = _queue_redirect()
    result = apply.run()
    assert ("drop", {f"{SITE}/blog/dead"}) in calls
    assert ("add", "/blog/dead", "/blog/live", True) in calls
    assert ledger.get(item_id).status == ledger.Status.SHIPPED
    assert result.urls == {f"{SITE}/blog/live"}            # the destination, never the dead source


def test_apply_drops_a_reclaim_decided_on_stale_liveness(applier):
    """Three days is enough for someone to have restored the page by hand."""
    from autoseo.act import ledger

    apply, calls = applier
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=4)).isoformat(timespec="seconds")
    item_id = _queue_redirect(created=old)
    apply.run()
    assert not [c for c in calls if c[0] == "add"]
    assert ledger.get(item_id).status == ledger.Status.DROPPED
