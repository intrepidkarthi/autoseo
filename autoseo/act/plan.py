"""Decide what to do, compose it, and hold it to the quality bar. Publishes nothing.

This half runs in the composing environment, which holds the model key and no repository
credential. It reads the open web — our own live pages, and Gemini's grounding results — so by
construction the worst outcome of anything it reads is a bad row in the ledger, not a commit.

Everything it produces has already cleared the gate by the time it lands in the ledger. `apply`
does not re-judge quality; it executes.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from autoseo.act import ledger, onpage, policy
from autoseo.core.log import get_logger

log = get_logger(__name__)

# Stop after this many composition attempts fail in a row. They almost always fail for the same
# reason — a missing key, a model outage — and walking the rest of the list just spends quota
# reproducing one error.
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class Planned:
    posts: int = 0
    meta: int = 0
    faq: int = 0
    pruned: int = 0            # pages to be switched off
    merged: int = 0            # duplicate pages to be folded into their better half
    sitemap_dropped: int = 0   # URLs to stop submitting
    skipped: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.skipped = self.skipped or []

    @property
    def total(self) -> int:
        return self.posts + self.meta + self.faq


def _live(url: str) -> bool:
    try:
        r = httpx.head(url, timeout=20.0, follow_redirects=True,
                       headers={"user-agent": "autoseo"})
        return r.status_code < 400
    except Exception:  # noqa: BLE001 — unreachable is not the same as absent; treat it as present
        return True


def run(days: int = 90, dry_run: bool = False) -> Planned:
    result = Planned()

    if reason := policy.paused():
        print(f"  paused — {reason}")
        result.skipped.append(f"paused: {reason}")
        return result

    # The duplication check is only real if the corpus is. An empty index passes everything.
    try:
        from autoseo.quality import plagiarism
        if n := plagiarism.refresh_from_live():
            print(f"  corpus: re-indexed {n} live page(s)")
    except Exception as exc:  # noqa: BLE001 — a stale corpus is survivable; a dead run is not
        log.warning("corpus refresh failed: %s", exc)

    _plan_hygiene(days, result, dry_run)
    _plan_onpage(days, result, dry_run)
    _plan_posts(days, result, dry_run)
    return result


# --- subtraction ---------------------------------------------------------------------------------

def _plan_hygiene(days: int, result: Planned, dry_run: bool) -> None:
    """What to stop submitting and what to stop publishing.

    Detected here rather than in `apply` because working out whether a sitemap URL still resolves
    means fetching it, and `apply` holds the publishing credential and reads nothing from the web.
    The boundary is worth one extra ledger row.
    """
    from autoseo.decide import prune
    from autoseo.publish import sitemap as sm

    if ledger.planned(ledger.Kind.PRUNE) or ledger.planned(ledger.Kind.SITEMAP):
        return

    # --- clusters that earn nothing
    for cluster in prune.dead_clusters(days):
        paths = sorted(u.replace("https://getdailyvox.com", "") for u in cluster.urls)
        print(f"\n  prune /blog/{cluster.prefix}*")
        print(f"      {cluster.evidence}")
        result.pruned += cluster.pages
        if dry_run:
            continue
        ledger.plan(ledger.Item(
            kind=ledger.Kind.PRUNE, title=f"noindex /blog/{cluster.prefix}* ({cluster.pages} pages)",
            body="\n".join(paths), rationale=cluster.evidence,
            meta={"prefix": cluster.prefix, "paths": paths,
                  "urls": sorted(cluster.urls), "pages": cluster.pages},
        ))

    # --- duplicate pages. One per run: it is the only change a reader can notice going wrong,
    # and shipping several at once makes the effect of any of them unreadable.
    from autoseo.decide import consolidate
    if not ledger.planned(ledger.Kind.MERGE):
        merges = consolidate.candidates(days)
        if merges:
            m = merges[0]
            print(f"\n  merge {m.loser_path} -> {m.winner_path}")
            print(f"      {m.evidence}")
            result.merged += 1
            if not dry_run:
                ledger.plan(ledger.Item(
                    kind=ledger.Kind.MERGE,
                    title=f"301 {m.loser_path} -> {m.winner_path}",
                    body=m.evidence, rationale=m.evidence,
                    meta={"source": m.loser_path, "destination": m.winner_path,
                          "loser": m.loser, "shared_queries": m.shared_queries,
                          "shared_impressions": round(m.shared_impressions)},
                ))
        if len(merges) > 1:
            print(f"      ({len(merges) - 1} more duplicate pair(s) waiting for a later run)")

    # --- URLs that should not be in a sitemap at all
    #
    # Candidates come from the URL Inspection data rather than from probing every entry. Google has
    # already fetched all 148 of them and recorded what it got, so re-checking each one daily would
    # be 148 requests to learn something the database already holds — and Google's verdict is the
    # one that matters anyway. Only the handful it flagged get verified over HTTP before removal,
    # because a sitemap entry is not worth deleting on stale data.
    from autoseo.core.db import session as _session

    with _session() as conn:
        suspect = [
            r["url"] for r in conn.execute(
                """SELECT u.url FROM url_inventory u JOIN url_index_status i ON i.url = u.url
                   WHERE u.in_sitemap = 1 AND (
                         LOWER(i.coverage_state) LIKE '%not found%'
                      OR LOWER(i.coverage_state) LIKE '%redirect%'
                      OR LOWER(i.coverage_state) LIKE '%excluded%')"""
            )
        ]

    dead = {u for u in suspect if not _live(u)}

    pagination: set[str] = set()
    for name in ("blog", "core"):
        try:
            xml = httpx.get(f"https://getdailyvox.com/sitemap-{name}.xml", timeout=30.0).text
        except Exception as exc:  # noqa: BLE001 — hygiene is not worth failing a run over
            log.warning("could not read sitemap-%s.xml: %s", name, str(exc)[:70])
            continue
        pagination |= {u for u in sm.urls(xml) if prune._PAGINATION.search(u)}

    if not (dead or pagination):
        return

    reasons = []
    if dead:
        reasons.append(f"{len(dead)} URL(s) that no longer resolve")
    if pagination:
        reasons.append(f"{len(pagination)} paginated listing(s)")
    rationale = (
        "A sitemap is a set of assertions that each URL is worth indexing. "
        + " and ".join(reasons)
        + " are wrong assertions, and Google reads a wrong assertion as a reason to trust the rest "
          "of the file less. Pagination is navigation, not content."
    )
    print(f"\n  sitemap: {', '.join(reasons)}")
    for u in sorted(dead | pagination)[:8]:
        print(f"      {u}")
    result.sitemap_dropped = len(dead) + len(pagination)
    if dry_run:
        return
    ledger.plan(ledger.Item(
        kind=ledger.Kind.SITEMAP, title=f"drop {result.sitemap_dropped} URL(s) from the sitemaps",
        body="\n".join(sorted(dead | pagination)), rationale=rationale,
        meta={"urls": sorted(dead | pagination), "dead": sorted(dead),
              "pagination": sorted(pagination)},
    ))


# --- fixes to pages that already exist ---------------------------------------------------------

def _plan_onpage(days: int, result: Planned, dry_run: bool) -> None:
    budget = policy.onpage_budget()
    if budget <= 0:
        result.skipped.append("on-page: fixes already composed and waiting")
        return

    cooling = policy.cooling_down()
    # An outage makes every candidate fail identically. Without this the run would walk the whole
    # list burning free-tier quota to produce the same error twenty times.
    failures = 0
    for c in onpage.candidates(days):
        if budget <= 0 or failures >= MAX_CONSECUTIVE_FAILURES:
            break
        if c.slug in cooling:
            continue
        try:
            live = onpage.live_page(c.url)
        except Exception as exc:  # noqa: BLE001 — one dead URL must not end the run
            log.warning("could not read %s: %s", c.url, str(exc)[:80])
            continue
        title, description, text = live.title, live.description, live.text

        if c.kind == "faq":
            if live.has_faq:
                continue
            try:
                block = onpage.compose_faq(c, text)
            except Exception as exc:  # noqa: BLE001 — a model outage skips a fix, not the run
                log.error("faq compose failed for %s: %s", c.slug, exc)
                result.skipped.append(f"faq {c.slug}: {str(exc)[:80]}")
                failures += 1
                continue
            failures = 0
            if not block:
                result.skipped.append(f"faq {c.slug}: nothing cleared the checks")
                continue
            print(f"\n  FAQ for /blog/{c.slug}")
            print(f"      {c.evidence}")
            if dry_run:
                for line in block.splitlines()[:14]:
                    print(f"      {line}")
            else:
                ledger.plan(ledger.Item(
                    kind=ledger.Kind.FAQ, title=f"FAQ section for /blog/{c.slug}", body=block,
                    rationale=c.evidence,
                    meta={"slug": c.slug, "url": c.url, "query": c.query,
                          "block": block, "est_click_gain": round(c.est_click_gain, 1)},
                ))
            result.faq += 1
        else:
            try:
                composed = onpage.compose_meta(c, title, description, text)
            except Exception as exc:  # noqa: BLE001 — a model outage skips a fix, not the run
                log.error("meta compose failed for %s: %s", c.slug, exc)
                result.skipped.append(f"meta {c.slug}: {str(exc)[:80]}")
                failures += 1
                continue
            failures = 0
            if not composed:
                result.skipped.append(f"meta {c.slug}: nothing cleared the checks")
                continue
            new_title, new_desc = composed
            print(f"\n  retitle /blog/{c.slug}")
            print(f"      {c.evidence}")
            print(f"      was : {title}")
            print(f"      now : {new_title}")
            print(f"      desc: {new_desc}")
            if not dry_run:
                ledger.plan(ledger.Item(
                    kind=ledger.Kind.META, title=f"Retitle /blog/{c.slug}",
                    body=f"{new_title}\n{new_desc}", rationale=c.evidence,
                    meta={"slug": c.slug, "url": c.url, "query": c.query,
                          "title": new_title, "description": new_desc,
                          "was_title": title, "was_description": description,
                          "est_click_gain": round(c.est_click_gain, 1)},
                ))
            result.meta += 1
        budget -= 1


# --- new posts ---------------------------------------------------------------------------------

def _plan_posts(days: int, result: Planned, dry_run: bool) -> None:
    from autoseo.compose import blog as composer
    from autoseo.compose.blog import _slugify
    from autoseo.decide import brief

    budget, why = policy.post_budget()
    if budget <= 0:
        print(f"\n  no post this run — {why}")
        result.skipped.append(f"post: {why}")
        return

    # No corpus means the duplication check passes everything, and this site's whole problem was
    # 1,500 near-identical pages. Refuse to write rather than write unchecked. On-page fixes above
    # are unaffected: they edit a page rather than adding one.
    from autoseo.quality import plagiarism
    if not plagiarism.corpus_size():
        print("\n  no post this run — the duplication corpus is empty, so nothing can be checked")
        result.skipped.append("post: duplication corpus is empty (site fetch failed?)")
        return

    # A query where nothing of ours is close enough to edit is what deserves a new page. Where a
    # blog post already ranks between 10 and 40, the on-page fixer above is the cheaper move, and
    # writing a second article for the same query would put two of our pages in competition.
    written = ledger.slugs_touched((ledger.Kind.POST,), days=3650)
    gsc = [a for a in brief.build(days=days) if a.kind == "too-far" or "/blog/" not in a.target]
    aeo = brief.aeo_gaps(days=days)

    # Alternate. Google-measured demand is the better-evidenced channel and would win every ranking
    # forever, so after two GSC-driven posts the next one goes to an answer-engine gap. Two thirds
    # / one third, decided by what actually shipped rather than by a counter that can drift.
    recent = ledger.recent_sources(ledger.Kind.POST, limit=2)
    prefer_aeo = bool(aeo) and len(recent) >= 2 and all(s == "gsc" for s in recent)
    wanted = [*aeo, *gsc] if prefer_aeo else [*gsc, *aeo]
    if prefer_aeo:
        print("\n  last two posts came from Search Console — this one targets an answer-engine gap")

    failures = 0
    for action in wanted:
        if budget <= 0 or failures >= MAX_CONSECUTIVE_FAILURES:
            break
        slug = _slugify(action.query)
        if slug in written:
            continue
        if _live(f"https://getdailyvox.com/blog/{slug}"):
            log.info("/blog/%s is already live — skipping", slug)
            continue

        # Do we already rank for this? Checking the slug and the URL only catches a page with the
        # same name; it does not catch a page with a different name written for the same query.
        # Without this the loop published /blog/voice-journaling-app as the *third* page competing
        # for "voice journaling app", the other two already sitting together at position 42.2.
        # A new page for a query we rank for does not add a competitor to the SERP, it adds one to
        # ourselves — and where a page already ranks, the on-page fixer is the cheaper move anyway.
        if ours := brief.pages_ranking_for(action.query, days=days):
            page, imp, pos = ours[0]
            log.info("skipping '%s': %s already ranks (%.0f imp, position %.1f)",
                     action.query, page.replace("https://getdailyvox.com", ""), imp, pos)
            result.skipped.append(
                f"post '{action.query[:40]}': {len(ours)} of our page(s) already rank for it"
            )
            continue

        source = "aeo" if action.kind == "aeo-gap" else "gsc"
        where = ("answer engines" if source == "aeo"
                 else f"Search Console, position {action.position:.1f}")
        print(f"\n  drafting for '{action.query[:70]}' ({where})")
        try:
            draft = composer.write(action)
        except Exception as exc:  # noqa: BLE001 — a model outage must not fail the whole run
            log.error("draft failed for '%s': %s", action.query, exc)
            result.skipped.append(f"post '{action.query}': {str(exc)[:80]}")
            failures += 1
            continue
        failures = 0
        if not draft:
            result.skipped.append(f"post '{action.query}': failed the quality gate twice")
            continue

        print(f"    {draft.title}")
        print(f"    {draft.verdict.summary()}")
        if dry_run:
            print(f"\n{draft.markdown[:600]}\n    ...")
            result.posts += 1
            budget -= 1
            continue

        ledger.plan(ledger.Item(
            kind=ledger.Kind.POST, title=draft.title, body=draft.markdown[:3000],
            rationale=f"{action.evidence} | {draft.verdict.summary()}",
            meta={"slug": draft.slug, "query": action.query, "markdown": draft.markdown,
                  "description": draft.description, "evidence": action.evidence,
                  "verdict": draft.verdict.summary(), "source": source,
                  "est_click_gain": round(action.est_click_gain, 1)},
        ))
        result.posts += 1
        budget -= 1
