"""autoseo command line.

The loop:

    autoseo run [--dry-run]                 measure, decide, fix, publish — the whole cycle
    autoseo plan [--dry-run] [--days N]     decide and compose; publishes nothing
    autoseo apply [--dry-run]               commit what plan composed, to the site repo
    autoseo status                          caps, ledger, and what happens on the next run

Measurement:

    autoseo inventory [--public-dir PATH]   build the URL list (sitemap + optional local public/)
    autoseo gsc [--days N] [--backfill]     pull Search Console search analytics
    autoseo inspect [--limit N]             rotate through URL Inspection (sitemap URLs only)
    autoseo bing                            pull Bing Webmaster stats
    autoseo collect                         gsc + bing + inspect + report
    autoseo report                          per-cluster indexation ratio
    autoseo diagnose                        isolate where GSC impressions go missing

Decision:

    autoseo brief [--days N] [--top N]      ranked actions with evidence
    autoseo opportunity [--days N]          striking distance, CTR gaps, content gaps
    autoseo aeo [--tier core|extended|all]  ask buyer questions, record what gets cited
    autoseo outreach [--days N]             pages worth getting listed on

Site and quality:

    autoseo dashboard [--out FILE]          render the whole measurement as one HTML page
    autoseo relink [--dry-run]              link live blog pages the index has orphaned
    autoseo agent-layer [--apply]           point AI agents at /llms.txt from the page itself
    autoseo delist [--apply]                noindex the de-listed clusters
    autoseo check FILE                      run the quality gate over a draft
    autoseo index-corpus [--from-live]      shingle the site for duplication checks

State:

    autoseo snapshot / restore              database <-> state/*.csv (git-mergeable)

Parked (video and social are switched off; the code stays, nothing schedules it):

    autoseo video --topic "..."             generate a Short (script + render), locally
    autoseo youtube-auth                    one-time YouTube OAuth, run locally
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from autoseo.core.config import ConfigError, settings
from autoseo.core.db import session
from autoseo.core.log import get_logger

log = get_logger("autoseo")


def _record_run(command: str, ok: bool, detail: str = "") -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO run_log(started, command, ok, detail) VALUES (?,?,?,?)",
            (dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), command, int(ok), detail[:2000]),
        )


def _print_report() -> None:
    from autoseo.collect.inspect import report

    rows = report()
    if not rows:
        print("\nNo inventory yet — run `autoseo inventory` first.\n")
        return

    print()
    print(f"{'cluster':<14}{'total':>8}{'sitemap':>9}{'checked':>9}{'indexed':>9}  {'indexed %':>10}")
    print("-" * 62)
    totals = {"total": 0, "in_sitemap": 0, "checked": 0, "indexed": 0}
    for r in rows:
        pct = (r["indexed"] / r["checked"] * 100) if r["checked"] else 0.0
        flag = ""
        # De-listed clusters: shown for context, excluded from the daily rotation.
        if r["total"] and not r["in_sitemap"]:
            flag = "  de-listed (not tracked)"
        print(
            f"{r['cluster']:<14}{r['total']:>8}{r['in_sitemap']:>9}"
            f"{r['checked']:>9}{r['indexed']:>9}  {pct:>9.1f}%{flag}"
        )
        for k in totals:
            totals[k] += r[k] or 0
    print("-" * 62)
    overall = (totals["indexed"] / totals["checked"] * 100) if totals["checked"] else 0.0
    print(
        f"{'TOTAL':<14}{totals['total']:>8}{totals['in_sitemap']:>9}"
        f"{totals['checked']:>9}{totals['indexed']:>9}  {overall:>9.1f}%"
    )
    print(f"\nRotation covers the {totals['in_sitemap']} sitemap URLs. "
          f"De-listed clusters are sampled once, not tracked.")
    print()


def _print_opportunities(days: int) -> None:
    from autoseo.decide import opportunity

    result = opportunity.report(days)
    titles = {
        "striking_distance": f"STRIKING DISTANCE — volume at position 8-30, last {days}d",
        "ctr_underperformers": "CTR UNDERPERFORMERS — ranked well, not clicked",
        "content_gaps": "CONTENT GAPS — demand with nothing ranking",
    }
    for key, title in titles.items():
        items = result[key]
        print(f"\n=== {title} ===")
        if not items:
            print("  (none)")
            continue
        for o in items[:12]:
            label = (o.page or o.query).replace("https://getdailyvox.com", "") or "/"
            print(f"  {o.impressions:>6.0f} imp {o.clicks:>4.0f} clk  pos {o.position:>5.1f}  {label[:48]}")
            print(f"         {o.rationale}")
    print()


def _print_outreach(days: int, top: int) -> None:
    from autoseo.decide import outreach

    targets = outreach.build(days)
    print(f"\n=== OUTREACH TARGETS — cited by answer engines, last {days}d ===")
    if not targets:
        print("  No citation data yet. Run `autoseo aeo` first.\n")
        return
    for t in targets[:top]:
        flag = "" if not t.we_are_listed else "  [already lists us]"
        print(f"\n  [{t.rank}] {t.domain}{flag}")
        print(f"      {t.title[:70]}")
        print(f"      {t.url[:95]}")
        print(f"      why   : {t.why}")
        if t.competitors_named:
            print(f"      names : {', '.join(t.competitors_named)}")
        print(f"      angle : {t.angle}")
    print()


def _print_brief(days: int, top: int) -> None:
    from autoseo.decide import brief

    actions = brief.build(days)
    print(f"\n=== ACTIONS — ranked by estimated click gain, last {days}d ===")
    if not actions:
        print("  Nothing actionable. Either there is no data yet, or no query has enough demand.")
    for a in actions[:top]:
        tgt = a.target.replace("https://getdailyvox.com", "") or "(none)"
        print(f"\n  [{a.priority}] {a.kind}  ~+{a.est_click_gain:.0f} clicks/90d")
        print(f"      query : {a.query}")
        print(f"      page  : {tgt}")
        print(f"      why   : {a.evidence}")
        for s in a.steps:
            print(f"        - {s}")

    canni = brief.cannibalised(days)
    print("\n=== COMPETING WITH OURSELVES — two or more pages on one query ===")
    if not canni:
        print("  None. Every acquisition query has one page behind it.")
    for c in canni[:top]:
        print(f"\n  '{c.query}'  —  {c.impressions:.0f} imp, best position {c.best_position:.1f}")
        for page, imp, pos in c.pages:
            print(f"      {imp:>5.0f} imp  pos {pos:>5.1f}  "
                  f"{page.replace('https://getdailyvox.com', '')}")

    gaps = brief.aeo_gaps(days)
    print("\n=== ANSWER-ENGINE GAPS — asked, competitors named, we are not ===")
    if not gaps:
        print("  None recorded. Run `autoseo aeo` to measure.")
    for g in gaps[:top]:
        print(f"\n  [{g.priority}] {g.query}")
        print(f"      {g.evidence}")

    ex = brief.excluded(days)
    print("\n=== EXCLUDED from acquisition analysis ===")
    for kind, items in ex.items():
        if not items:
            continue
        total = sum(i for _, i in items)
        names = ", ".join(q for q, _ in items[:4])
        print(f"  {kind:<12}{total:>6.0f} imp across {len(items):>3}  ({names})")
    print()


def _print_status() -> None:
    from autoseo.act import ledger, policy
    from autoseo.collect import health
    from autoseo.quality import plagiarism

    # First, because it is the ceiling on everything below it. A page that is not indexed cannot
    # rank however good its title is.
    print("\n=== INDEXATION — the ceiling on everything else ===")
    print(health.describe())

    print("\n=== POLICY — what the loop is allowed to do next ===")
    print(policy.describe())

    print("\n=== LEDGER — last 30 days ===")
    rows = ledger.summary(days=30)
    if not rows:
        print("  nothing planned or shipped")
    for kind, status, n in rows:
        print(f"  {kind:<10}{status:<10}{n:>4}")

    recent = ledger.recent(limit=8)
    if recent:
        print("\n=== RECENT ===")
        for item in recent:
            when = (item.decided_at or item.created or "")[:16]
            url = item.meta.get("commit") or item.meta.get("error", "")
            print(f"  {when}  {item.status:<8}{item.kind:<7}{item.title[:44]}")
            if url:
                print(f"      {str(url)[:100]}")

    n = plagiarism.corpus_size()
    stamp = plagiarism.indexed_at() or "never"
    print(f"\n  duplication corpus: {n} page(s), indexed {stamp}")
    if not n:
        print("  WARNING: the corpus is empty, so the duplication check passes everything.")
    print()


def _run_video(args) -> None:
    """Script -> voiceover -> footage -> render. Parked: nothing schedules this."""
    from autoseo.compose import video as composer

    spec = composer.write(args.topic, search_terms=args.terms)
    if not spec:
        print("  no script cleared the gate — nothing rendered")
        return

    print(f"\n  {spec.words} words\n")
    print("  " + spec.script.replace(". ", ".\n  "))
    if args.script_only:
        return

    from autoseo.media import footage, render, speech

    work = args.out.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    segments = speech.synthesise(spec.script, work / "voice.wav")
    srt = speech.write_srt(segments, work / "captions.srt")
    clips = footage.fetch(spec.search_terms, count=max(3, len(segments) // 2), out_dir=work)
    out = render.render(clips, work / "voice.wav", srt, args.out, segments)
    print(f"\n  rendered {out}  ({out.stat().st_size / 1_048_576:.1f} MB)")
    print("  upload is manual while video is parked.")


def _run_loop(args) -> None:
    """The whole cycle in one command: measure, decide, compose, ship.

    CI splits this across two jobs so the composing half never holds the publishing token. Run
    locally it is one process, which needs every credential at once — that is the difference, and
    the only one.
    """
    from autoseo.act import apply as applier
    from autoseo.act import plan as planner

    if not args.skip_collect:
        from autoseo.collect import bing, gsc, health, inspect, inventory
        for label, fn in (
            ("inventory", lambda: inventory.build(None)),
            ("gsc", lambda: gsc.collect(days=10)),
            ("bing", bing.collect),
            ("inspect", lambda: inspect.collect(limit=None)),
            ("health", health.record),
        ):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — measure what you can; decide on what you have
                log.error("%s failed: %s", label, exc)
                _record_run(f"run/{label}", ok=False, detail=repr(exc))
                print(f"  WARNING: {label} failed ({str(exc)[:90]}) — continuing on stored data")
        _print_report()

    print("\n=== PLAN ===")
    planned = planner.run(days=args.days, dry_run=args.dry_run)
    print(f"\n  planned: {planned.posts} post(s), {planned.meta} retitle(s), {planned.faq} FAQ(s)")
    for s in planned.skipped:
        print(f"  skipped: {s}")

    print("\n=== APPLY ===")
    applied = applier.run(dry_run=args.dry_run)
    print(f"\n  shipped {len(applied.shipped)}, failed {len(applied.failed)}, "
          f"dropped {len(applied.dropped)}")
    for f in applied.failed:
        print(f"  FAILED: {f}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoseo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    # --- the loop ---------------------------------------------------------------------------
    p_run = sub.add_parser("run", help="measure, decide, fix, publish — the whole cycle")
    p_run.add_argument("--days", type=int, default=90, help="analysis window")
    p_run.add_argument("--dry-run", action="store_true", help="decide and print, change nothing")
    p_run.add_argument("--skip-collect", action="store_true",
                       help="use stored measurement instead of pulling fresh data")

    p_plan = sub.add_parser("plan", help="decide and compose; publishes nothing")
    p_plan.add_argument("--days", type=int, default=90)
    p_plan.add_argument("--dry-run", action="store_true")

    p_apply = sub.add_parser("apply", help="commit what plan composed, to the site repo")
    p_apply.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="caps, ledger, and what happens on the next run")

    # --- measurement ------------------------------------------------------------------------
    p_inv = sub.add_parser("inventory", help="build the URL inventory")
    p_inv.add_argument("--public-dir", type=Path, default=None,
                       help="local path to the site's public/ dir, to find URLs missing from the sitemap")

    p_gsc = sub.add_parser("gsc", help="pull Search Console search analytics")
    p_gsc.add_argument("--days", type=int, default=10)
    p_gsc.add_argument("--backfill", action="store_true",
                       help="walk the full 16-month retention window in monthly chunks")

    p_ins = sub.add_parser("inspect", help="rotate through the URL Inspection API")
    p_ins.add_argument("--limit", type=int, default=None)
    p_ins.add_argument("--sample-orphans", type=int, default=0, metavar="N",
                       help="one-time: inspect N de-listed URLs per cluster to decide noindex vs 410")

    p_diag = sub.add_parser("diagnose", help="isolate where GSC impressions go missing")
    p_diag.add_argument("--start", default="2026-04-30")
    p_diag.add_argument("--end", default="2026-07-29")

    sub.add_parser("bing", help="pull Bing Webmaster stats")
    sub.add_parser("report", help="print the indexation report")

    p_all = sub.add_parser("collect", help="gsc + bing + inspect + report")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--days", type=int, default=10)

    # --- decision ---------------------------------------------------------------------------
    p_brief = sub.add_parser("brief", help="ranked actions with evidence — the decision, not the data")
    p_brief.add_argument("--days", type=int, default=90)
    p_brief.add_argument("--top", type=int, default=8)

    p_opp = sub.add_parser("opportunity", help="where the leverage is: striking distance, CTR, gaps")
    p_opp.add_argument("--days", type=int, default=90)

    p_aeo = sub.add_parser("aeo", help="run the buyer-question panel against Gemini grounding")
    # `frontier` is deliberately absent from the workflow's rotation: it measures a cluster the site
    # has no foothold in, so it answers a strategy question rather than tracking a trend, and it is
    # run when someone wants that answer.
    p_aeo.add_argument("--tier", default="core",
                       choices=["core", "extended", "frontier", "all"])
    p_aeo.add_argument("--repeats", type=int, default=3)
    p_aeo.add_argument("--dry-run", action="store_true")
    p_aeo.add_argument("--list-models", action="store_true")
    p_aeo.add_argument("--model", default=None)

    p_out = sub.add_parser("outreach", help="pages worth getting listed on, ranked")
    p_out.add_argument("--days", type=int, default=30)
    p_out.add_argument("--top", type=int, default=10)

    # --- site and quality -------------------------------------------------------------------
    p_relink = sub.add_parser(
        "relink", help="find live blog pages the index links to nowhere, and link them"
    )
    p_relink.add_argument("--dry-run", action="store_true")

    p_agent = sub.add_parser(
        "agent-layer", help="point AI agents at /llms.txt from the page itself"
    )
    p_agent.add_argument("--apply", action="store_true",
                         help="commit the note to every blog page (default: report coverage)")
    p_agent.add_argument("--sync-profile", type=Path, metavar="FILE",
                         help="ship a locally-authored llms.txt / llms-full.txt to the site")
    p_agent.add_argument("--dry-run", action="store_true")

    p_del = sub.add_parser("delist", help="noindex the orphaned page clusters")
    p_del.add_argument("--apply", action="store_true",
                       help="commit the headers to the site (default: print the plan)")
    p_del.add_argument("--dry-run", action="store_true")

    p_prune = sub.add_parser(
        "prune", help="blog clusters that earn nothing, and sitemap URLs that should not be there"
    )
    p_prune.add_argument("--days", type=int, default=90)

    p_dash = sub.add_parser("dashboard", help="render the measurement as one HTML page")
    p_dash.add_argument("--out", type=Path, default=Path("state/dashboard.html"))

    p_chk = sub.add_parser("check", help="run the quality gate over a file")
    p_chk.add_argument("path", type=Path)

    p_idx = sub.add_parser("index-corpus", help="shingle the existing site for duplication checks")
    p_idx.add_argument("--public-dir", type=Path, default=None,
                       help="local checkout of the site's public/ directory")
    p_idx.add_argument("--from-live", action="store_true",
                       help="fetch the sitemap URLs over HTTP instead (what CI uses)")
    p_idx.add_argument("--force", action="store_true", help="re-index even if it is fresh")

    # --- state ------------------------------------------------------------------------------
    sub.add_parser("snapshot", help="write state/*.csv from the database")
    sub.add_parser("restore", help="rebuild the database from state/*.csv")

    # --- parked -----------------------------------------------------------------------------
    p_vid = sub.add_parser("video", help="[parked] generate a Short: script, voiceover, render")
    p_vid.add_argument("--topic", required=True)
    p_vid.add_argument("--terms", default="journal writing calm morning routine",
                       help="stock footage search terms")
    p_vid.add_argument("--out", type=Path, default=Path("state/media/short.mp4"))
    p_vid.add_argument("--script-only", action="store_true", help="write the script, skip rendering")

    p_yt = sub.add_parser("youtube-auth", help="[parked] one-time YouTube OAuth, run locally")
    p_yt.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    p_yt.add_argument("--check", action="store_true", help="describe an existing token.json")

    args = parser.parse_args(argv)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    exit_code = 0

    try:
        if args.command == "run":
            _run_loop(args)

        elif args.command == "plan":
            from autoseo.act import plan as planner
            result = planner.run(days=args.days, dry_run=args.dry_run)
            print(f"\n  planned: {result.posts} post(s), {result.meta} retitle(s), "
                  f"{result.faq} FAQ(s)")
            for s in result.skipped:
                print(f"  skipped: {s}")
            print()

        elif args.command == "apply":
            from autoseo.act import apply as applier
            result = applier.run(dry_run=args.dry_run)
            print(f"\n  shipped {len(result.shipped)}, failed {len(result.failed)}, "
                  f"dropped {len(result.dropped)}")
            for f in result.failed:
                print(f"  FAILED: {f}")
            print()
            exit_code = 1 if result.failed else 0

        elif args.command == "status":
            _print_status()

        elif args.command == "inventory":
            from autoseo.collect import inventory
            counts = inventory.build(args.public_dir)
            for cluster, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {cluster:<14}{n:>7}")
            print(f"  {'TOTAL':<14}{sum(counts.values()):>7}")

        elif args.command == "gsc":
            from autoseo.collect import gsc
            if args.backfill:
                gsc.backfill()
            else:
                gsc.collect(days=args.days)

        elif args.command == "opportunity":
            _print_opportunities(args.days)

        elif args.command == "aeo":
            from autoseo.aeo import probe
            if args.list_models:
                probe.list_models()
            else:
                probe.run(tier=args.tier, repeats=args.repeats, dry_run=args.dry_run,
                          model=args.model or probe.DEFAULT_MODEL)

        elif args.command == "outreach":
            _print_outreach(args.days, args.top)

        elif args.command == "brief":
            _print_brief(args.days, args.top)

        elif args.command == "inspect":
            from autoseo.collect import health, inspect
            inspect.collect(limit=args.limit, sample_orphans=args.sample_orphans)
            health.record()
            _print_report()

        elif args.command == "diagnose":
            from autoseo.collect import diagnose
            diagnose.run(args.start, args.end)

        elif args.command == "bing":
            from autoseo.collect import bing
            bing.collect()

        elif args.command == "report":
            _print_report()

        elif args.command == "relink":
            from autoseo.publish import blog as publisher
            if url := publisher.relink(dry_run=args.dry_run):
                print(f"  {url}")

        elif args.command == "dashboard":
            from autoseo import dashboard
            path = dashboard.render(args.out)
            print(f"  {path}")

        elif args.command == "agent-layer":
            from autoseo.publish import agent_layer
            if args.sync_profile:
                if url := agent_layer.sync_profile(args.sync_profile, dry_run=args.dry_run):
                    print(f"  {url}")
            elif args.apply:
                if url := agent_layer.backfill(dry_run=args.dry_run):
                    print(f"  {url}")
            else:
                carrying, total = agent_layer.status()
                print(f"\n  {carrying}/{total} blog page(s) point agents at /llms.txt")
                if carrying < total:
                    print(f"  {total - carrying} to go — `autoseo agent-layer --apply` ships them.")
                # Reported here rather than only on publish: pointing agents at a stale profile is
                # worse than not pointing them at it, so the coverage number and the accuracy
                # warning belong on the same screen.
                if drift := agent_layer.audit_profile():
                    print(f"\n  STALE PROFILE: {drift}")
                print()

        elif args.command == "delist":
            from autoseo.publish import delist
            if args.apply:
                if url := delist.apply(dry_run=args.dry_run):
                    print(f"  {url}")
            else:
                print("\n" + delist.render_patch(delist.build_plan()) + "\n")

        elif args.command == "index-corpus":
            from autoseo.quality import plagiarism
            if args.public_dir:
                n = plagiarism.build_index(args.public_dir)
            else:
                n = plagiarism.refresh_from_live(max_age_days=0 if args.force else
                                                 plagiarism.CORPUS_MAX_AGE_DAYS)
            print(f"  indexed {n} pages" if n else "  corpus is already fresh")

        elif args.command == "prune":
            from autoseo.decide import prune as pruner
            dead = pruner.dead_clusters(args.days)
            print(f"\n=== CLUSTERS EARNING NOTHING — last {args.days}d ===")
            if not dead:
                print("  None. Every blog cluster clears the floor.")
            for c in dead:
                print(f"\n  /blog/{c.prefix}*")
                print(f"      {c.evidence}")
                for u in c.urls[:4]:
                    print(f"      {u.replace('https://getdailyvox.com', '')}")
                if len(c.urls) > 4:
                    print(f"      ... and {len(c.urls) - 4} more")
            print()

        elif args.command == "check":
            from autoseo.quality import gate
            v = gate.evaluate(args.path.read_text(encoding="utf-8"))
            print(f"\n  {v.summary()}\n")
            for r in v.reasons:
                print(f"    BLOCK  {r}")
            for w in v.warnings[:8]:
                print(f"    warn   {w}")
            print()
            exit_code = 0 if v.passed else 1

        elif args.command == "snapshot":
            from autoseo.core import snapshot
            for tbl, n in snapshot.dump().items():
                print(f"  {tbl:<20}{n:>7}")

        elif args.command == "restore":
            from autoseo.core import snapshot
            for tbl, n in snapshot.load().items():
                print(f"  {tbl:<20}{n:>7}")

        elif args.command == "collect":
            from autoseo.collect import bing, gsc, health, inspect
            gsc.collect(days=args.days)
            bing.collect()
            inspect.collect(limit=args.limit)
            health.record()
            _print_report()

        elif args.command == "video":
            _run_video(args)

        elif args.command == "youtube-auth":
            from autoseo.publish import youtube_auth
            if args.check:
                youtube_auth.describe_token()
            else:
                youtube_auth.authorise(args.client_secret)

    except ConfigError as exc:
        log.error("%s", exc)
        _record_run(args.command, ok=False, detail=str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - top level: log, record, exit non-zero
        log.exception("%s failed", args.command)
        _record_run(args.command, ok=False, detail=repr(exc))
        return 1

    _record_run(args.command, ok=exit_code == 0)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
