"""autoseo command line.

    autoseo inventory [--public-dir PATH]   build the URL list (sitemap + optional local public/)
    autoseo gsc [--days N]                  pull Search Console search analytics
    autoseo inspect [--limit N]             rotate through URL Inspection (sitemap URLs only)
    autoseo inspect --sample-orphans N      one-time sample of the de-listed clusters
    autoseo bing                            pull Bing Webmaster stats
    autoseo gsc --backfill                  pull the full 16-month history
    autoseo opportunity [--days N]          where the leverage is
    autoseo brief [--days N] [--top N]      ranked actions with evidence
    autoseo gate [--test-card|--status]     send cards, process approvals
    autoseo aeo [--tier core|extended|all]  ask buyer questions, record what gets cited
    autoseo outreach [--days N]             pages worth getting listed on
    autoseo report                          print the indexation report
    autoseo index-corpus --public-dir P     shingle the site for duplication checks
    autoseo draft [--top N] [--queue]       write posts against measured demand
    autoseo publish [--dry-run]             open PRs for approved drafts
    autoseo video --topic "..."             generate a Short (script + render)
    autoseo youtube-auth                    one-time YouTube OAuth, run locally
    autoseo delist                          plan the noindex for orphaned clusters
    autoseo check FILE                      run the quality gate over a draft
    autoseo snapshot / restore              state <-> state/*.csv (git-mergeable)
    autoseo collect                         gsc + bing + inspect + report  (what CI runs)
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


def _run_video(args) -> None:
    """Script -> voiceover -> footage -> render. Every step free."""
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

    if args.queue:
        import os as _os

        from autoseo.gate import cards, queue
        from autoseo.gate.queue import Item, get
        item_id = queue.add(Item(
            kind="video", channel="youtube", title=spec.title,
            body=spec.script, rationale=f"Topic: {spec.topic} | {spec.words} words",
            meta={
                "description": spec.description,
                "synthetic": True,
                # The publish job runs on a different runner, so it fetches the render from this
                # run's artifact rather than from a path that will not exist.
                "run_id": _os.environ.get("GITHUB_RUN_ID", ""),
                "artifact": "short",
            },
        ))
        try:
            cards.send_video_now(get(item_id), out)
            print("  sent to telegram for approval")
        except Exception as exc:  # noqa: BLE001 — queued regardless; the gate cron will retry
            print(f"  queued, but could not send now: {exc}")


def _run_draft(args) -> None:
    """Draft against the highest-value measured opportunities, and optionally queue for approval."""
    import json as _json

    from autoseo.compose import blog
    from autoseo.decide import brief
    from autoseo.gate import queue
    from autoseo.gate.queue import Item

    actions = [a for a in brief.build(days=90) if a.kind == "improve-page"][: args.top]
    if not actions:
        print("  No reachable opportunities. Nothing worth writing today.")
        return

    for action in actions:
        print(f"\n  drafting for '{action.query}' (position {action.position:.1f})")
        draft = blog.write(action)
        if not draft:
            print("    dropped — failed the quality gate twice")
            continue
        print(f"    {draft.title}")
        print(f"    {draft.verdict.summary()}")
        if args.dry_run:
            print(f"\n{draft.markdown[:500]}\n    ...")
            continue
        if args.queue:
            queue.add(Item(
                kind="blog", channel="blog", title=draft.title,
                body=draft.markdown[:3000],
                rationale=f"{action.evidence} | {draft.verdict.summary()}",
                meta={"slug": draft.slug, "query": action.query,
                      "markdown": draft.markdown, "description": draft.description,
                      "evidence": action.evidence},
            ))
            print("    queued for approval")


def _run_publish(args) -> None:
    """Open PRs for blog items that were approved in Telegram."""
    from autoseo.compose.blog import Draft
    from autoseo.gate import queue
    from autoseo.gate.queue import Status
    from autoseo.publish import blog as publisher
    from autoseo.quality import gate as qgate

    items = [i for i in queue.approved_unposted() if i.channel in ("blog", "youtube")]
    if not items:
        print("  Nothing approved and waiting.")
        return

    for item in items:
        if item.channel == "youtube":
            from autoseo.publish import youtube
            try:
                video = youtube.fetch_render(item.meta.get("run_id", ""),
                                             item.meta.get("artifact", "short"))
                vid = youtube.upload(
                    video, item.title, item.meta.get("description", ""),
                    privacy="private",
                    # The narration is machine-generated, so YouTube requires the disclosure.
                    synthetic=bool(item.meta.get("synthetic")),
                    dry_run=args.dry_run,
                )
            except Exception as exc:  # noqa: BLE001 — one bad item must not stop the rest
                log.error("youtube upload failed for %s: %s", item.title, exc)
                queue.decide(item.id, Status.FAILED, by="publish")
                continue
            if not args.dry_run:
                queue.decide(item.id, Status.POSTED, by="publish")
                print(f"  https://youtube.com/watch?v={vid}")
            continue

        meta = item.meta
        draft = Draft(
            slug=meta.get("slug", ""), title=item.title,
            description=meta.get("description", ""), markdown=meta.get("markdown", item.body),
            target_query=meta.get("query", ""), evidence=meta.get("evidence", ""),
            verdict=qgate.Verdict(passed=True),
        )
        try:
            url = publisher.publish(draft, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 — one bad item must not stop the rest
            log.error("publish failed for %s: %s", item.title, exc)
            queue.decide(item.id, Status.FAILED, by="publish")
            continue
        if not args.dry_run:
            queue.decide(item.id, Status.POSTED, by="publish")
            print(f"  {url}")


def _run_gate(args) -> None:
    from autoseo.gate import cards, client, queue
    from autoseo.gate.queue import Item

    if args.raw:
        # Deliberately does NOT advance the offset — inspecting the queue must not consume it.
        import json as _json
        from autoseo.gate.client import _call
        # Isolate which parameter suppresses the result: same method, four variants.
        for label, kw in [
            ("no params            ", {}),
            ("limit=100            ", {"limit": 100}),
            ("timeout=0,limit=100  ", {"timeout": 0, "limit": 100}),
            ("offset=31484907      ", {"timeout": 0, "limit": 100, "offset": 31484907}),
        ]:
            try:
                r = _call("getUpdates", **kw)
                print(f"    {label} -> {len(r)} update(s) {[u.get('update_id') for u in r]}")
            except Exception as exc:
                print(f"    {label} -> ERROR {exc}")
        updates = _call("getUpdates", limit=100)
        print(f"\n  {len(updates)} pending update(s)")
        for u in updates:
            kind = "callback_query" if "callback_query" in u else \
                   "message" if "message" in u else ",".join(k for k in u if k != "update_id")
            detail = ""
            if cb := u.get("callback_query"):
                detail = f" data={cb.get('data')!r} msg={(cb.get('message') or {}).get('message_id')}"
            elif m := u.get("message"):
                detail = f" text={(m.get('text') or '')[:30]!r}"
            print(f"    id={u['update_id']}  {kind}{detail}")
        print()
        return

    if args.status:
        print("\n" + client.dump_state())
        with_counts = {}
        from autoseo.core.db import session
        with session() as conn:
            for r in conn.execute("SELECT status, COUNT(*) n FROM queue_item GROUP BY status"):
                with_counts[r["status"]] = r["n"]
        print(f"  queue: {with_counts or 'empty'}\n")
        return

    if args.test_card:
        queue.add(Item(
            kind="test", channel="test", title="autoseo gate is live",
            body="If you can see this and the buttons work, the approval loop is connected. "
                 "Nothing publishes without a decision recorded here.",
            rationale="Sent by `autoseo gate --test-card` to verify end-to-end delivery.",
        ))

    if args.queue_outreach:
        from autoseo.decide import outreach
        from autoseo.gate import compose_outreach
        targets = outreach.build(days=30)
        drafts = compose_outreach.queue_top(targets, limit=args.queue_outreach)
        for d in drafts:
            queue.add(d)
        print(f"  queued {len(drafts)} outreach pitch(es)")

    if args.update:
        processed = cards.process_one(args.update)
    else:
        processed = cards.process_updates()
    sent = cards.send_pending()
    print(f"  decisions processed: {processed}   cards sent: {sent}")


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

    ex = brief.excluded(days)
    print("\n=== EXCLUDED from acquisition analysis ===")
    for kind, items in ex.items():
        if not items:
            continue
        total = sum(i for _, i in items)
        names = ", ".join(q for q, _ in items[:4])
        print(f"  {kind:<12}{total:>6.0f} imp across {len(items):>3}  ({names})")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoseo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_inv = sub.add_parser("inventory", help="build the URL inventory")
    p_inv.add_argument("--public-dir", type=Path, default=None,
                       help="local path to the site's public/ dir, to find URLs missing from the sitemap")

    p_gsc = sub.add_parser("gsc", help="pull Search Console search analytics")
    p_gsc.add_argument("--days", type=int, default=10)
    p_gsc.add_argument("--backfill", action="store_true",
                       help="walk the full 16-month retention window in monthly chunks")

    p_opp = sub.add_parser("opportunity", help="where the leverage is: striking distance, CTR, gaps")
    p_opp.add_argument("--days", type=int, default=90)

    p_aeo = sub.add_parser("aeo", help="run the buyer-question panel against Gemini grounding")
    p_aeo.add_argument("--tier", default="core", choices=["core", "extended", "all"])
    p_aeo.add_argument("--repeats", type=int, default=3)
    p_aeo.add_argument("--dry-run", action="store_true")
    p_aeo.add_argument("--list-models", action="store_true")
    p_aeo.add_argument("--model", default=None)

    p_gate = sub.add_parser("gate", help="send pending cards and process approvals")
    p_gate.add_argument("--test-card", action="store_true", help="queue and send one test card")
    p_gate.add_argument("--queue-outreach", type=int, default=0, metavar="N",
                        help="draft pitches for the top N outreach targets")
    p_gate.add_argument("--status", action="store_true")
    p_gate.add_argument("--update", metavar="JSON",
                        help="process one Telegram update handed in by the webhook dispatch "
                             "(a webhook disables getUpdates, so the payload must be passed in)")
    p_gate.add_argument("--raw", action="store_true",
                        help="dump what getUpdates actually returns, without consuming it")

    p_out = sub.add_parser("outreach", help="pages worth getting listed on, ranked")
    p_out.add_argument("--days", type=int, default=30)
    p_out.add_argument("--top", type=int, default=10)

    p_brief = sub.add_parser("brief", help="ranked actions with evidence — the decision, not the data")
    p_brief.add_argument("--days", type=int, default=90)
    p_brief.add_argument("--top", type=int, default=8)

    p_ins = sub.add_parser("inspect", help="rotate through the URL Inspection API")
    p_ins.add_argument("--limit", type=int, default=None)
    p_ins.add_argument("--sample-orphans", type=int, default=0, metavar="N",
                       help="one-time: inspect N de-listed URLs per cluster to decide noindex vs 410")

    p_diag = sub.add_parser("diagnose", help="isolate where GSC impressions go missing")
    p_diag.add_argument("--start", default="2026-04-30")
    p_diag.add_argument("--end", default="2026-07-29")

    sub.add_parser("bing", help="pull Bing Webmaster stats")
    sub.add_parser("report", help="print the indexation report")
    p_idx = sub.add_parser("index-corpus", help="shingle the existing site for duplication checks")
    p_idx.add_argument("--public-dir", type=Path, required=True)

    p_draft = sub.add_parser("draft", help="write a post against the top measured opportunity")
    p_draft.add_argument("--top", type=int, default=1, help="how many opportunities to draft for")
    p_draft.add_argument("--queue", action="store_true", help="send drafts to the Telegram gate")
    p_draft.add_argument("--dry-run", action="store_true")

    p_pub = sub.add_parser("publish", help="open PRs for approved drafts")
    p_pub.add_argument("--dry-run", action="store_true")

    p_vid = sub.add_parser("video", help="generate a Short: script, voiceover, footage, render")
    p_vid.add_argument("--topic", required=True)
    p_vid.add_argument("--terms", default="journal writing calm morning routine",
                       help="stock footage search terms")
    p_vid.add_argument("--out", type=Path, default=Path("state/media/short.mp4"))
    p_vid.add_argument("--script-only", action="store_true", help="write the script, skip rendering")
    p_vid.add_argument("--queue", action="store_true", help="send to the Telegram gate when done")

    p_yt = sub.add_parser("youtube-auth", help="one-time YouTube OAuth (run locally, not in CI)")
    p_yt.add_argument("--client-secret", type=Path, default=Path("client_secret.json"))
    p_yt.add_argument("--check", action="store_true", help="describe an existing token.json")

    sub.add_parser("delist", help="plan the noindex for the orphaned page clusters")

    p_chk = sub.add_parser("check", help="run the quality gate over a file")
    p_chk.add_argument("path", type=Path)

    sub.add_parser("snapshot", help="write state/*.csv from the database")
    sub.add_parser("restore", help="rebuild the database from state/*.csv")

    p_all = sub.add_parser("collect", help="gsc + bing + inspect + report")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--days", type=int, default=10)

    args = parser.parse_args(argv)
    settings.state_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.command == "inventory":
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

        elif args.command == "gate":
            _run_gate(args)

        elif args.command == "brief":
            _print_brief(args.days, args.top)

        elif args.command == "inspect":
            from autoseo.collect import inspect
            inspect.collect(limit=args.limit, sample_orphans=args.sample_orphans)
            _print_report()

        elif args.command == "diagnose":
            from autoseo.collect import diagnose
            diagnose.run(args.start, args.end)

        elif args.command == "bing":
            from autoseo.collect import bing
            bing.collect()

        elif args.command == "report":
            _print_report()

        elif args.command == "draft":
            _run_draft(args)

        elif args.command == "publish":
            _run_publish(args)

        elif args.command == "youtube-auth":
            from autoseo.publish import youtube_auth
            if args.check:
                youtube_auth.describe_token()
            else:
                youtube_auth.authorise(args.client_secret)

        elif args.command == "video":
            _run_video(args)

        elif args.command == "delist":
            from autoseo.publish import delist
            print("\n" + delist.render_patch(delist.build_plan()) + "\n")

        elif args.command == "index-corpus":
            from autoseo.quality import plagiarism
            n = plagiarism.build_index(args.public_dir)
            print(f"  indexed {n} pages")

        elif args.command == "check":
            from autoseo.quality import gate
            v = gate.evaluate(args.path.read_text(encoding="utf-8"))
            print(f"\n  {v.summary()}\n")
            for r in v.reasons:
                print(f"    BLOCK  {r}")
            for w in v.warnings[:8]:
                print(f"    warn   {w}")
            print()
            return 0 if v.passed else 1

        elif args.command == "snapshot":
            from autoseo.core import snapshot
            for tbl, n in snapshot.dump().items():
                print(f"  {tbl:<20}{n:>7}")

        elif args.command == "restore":
            from autoseo.core import snapshot
            for tbl, n in snapshot.load().items():
                print(f"  {tbl:<20}{n:>7}")

        elif args.command == "collect":
            from autoseo.collect import bing, gsc, inspect
            gsc.collect(days=args.days)
            bing.collect()
            inspect.collect(limit=args.limit)
            _print_report()

    except ConfigError as exc:
        log.error("%s", exc)
        _record_run(args.command, ok=False, detail=str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - top level: log, record, exit non-zero
        log.exception("%s failed", args.command)
        _record_run(args.command, ok=False, detail=repr(exc))
        return 1

    _record_run(args.command, ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
