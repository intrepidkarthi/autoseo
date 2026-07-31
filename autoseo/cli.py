"""autoseo command line.

    autoseo inventory [--public-dir PATH]   build the URL list (sitemap + optional local public/)
    autoseo gsc [--days N]                  pull Search Console search analytics
    autoseo inspect [--limit N]             rotate through URL Inspection (sitemap URLs only)
    autoseo inspect --sample-orphans N      one-time sample of the de-listed clusters
    autoseo bing                            pull Bing Webmaster stats
    autoseo gsc --backfill                  pull the full 16-month history
    autoseo opportunity [--days N]          where the leverage is
    autoseo report                          print the indexation report
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

    p_ins = sub.add_parser("inspect", help="rotate through the URL Inspection API")
    p_ins.add_argument("--limit", type=int, default=None)
    p_ins.add_argument("--sample-orphans", type=int, default=0, metavar="N",
                       help="one-time: inspect N de-listed URLs per cluster to decide noindex vs 410")

    sub.add_parser("bing", help="pull Bing Webmaster stats")
    sub.add_parser("report", help="print the indexation report")

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

        elif args.command == "inspect":
            from autoseo.collect import inspect
            inspect.collect(limit=args.limit, sample_orphans=args.sample_orphans)
            _print_report()

        elif args.command == "bing":
            from autoseo.collect import bing
            bing.collect()

        elif args.command == "report":
            _print_report()

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
