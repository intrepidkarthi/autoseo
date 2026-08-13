"""Execute the ledger against the site repo. Composes nothing, judges nothing.

This half runs in the publishing environment, which holds the only credential that can change
getdailyvox.com. It makes no model calls and reads nothing from the open web, so there is no path
from something the composer read to something this step writes. That separation was the point of
the two-environment design and it survives the removal of the human gate — it was never the gate
that provided it.

Failures are per item. An unattended pipeline that abandons four good actions because the fifth hit
a 409 is worse than no pipeline, because the failure is silent and the ledger still says "planned".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from autoseo.act import ledger, policy
from autoseo.core.log import get_logger

log = get_logger(__name__)

# A post composed against numbers this old should be re-decided, not shipped. The evidence that
# justified it has moved on, and nothing about the draft records that.
STALE_DAYS = 14


@dataclass
class Applied:
    shipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    urls: set[str] = field(default_factory=set)      # live URLs that changed
    submitted: int = 0                               # accepted by IndexNow

    @property
    def total(self) -> int:
        return len(self.shipped)


def _stale(item: ledger.Item) -> bool:
    if not item.created:
        return False
    age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(item.created)
    return age.days > STALE_DAYS


def run(dry_run: bool = False) -> Applied:
    from autoseo.compose.blog import Draft
    from autoseo.core.config import ConfigError, settings
    from autoseo.publish import blog as publisher
    from autoseo.publish import delist, indexnow, sitemap
    from autoseo.quality import gate as qgate

    result = Applied()

    if reason := policy.paused():
        print(f"  paused — {reason}")
        return result

    # Checked once, up front. Every step below would otherwise fail on the same missing token, and
    # five identical failures in the ledger read like five different problems.
    if not settings.gh_dailyvox_token:
        raise ConfigError(
            "GH_DAILYVOX_TOKEN is not set. `apply` is the only step that writes to "
            "getdailyvox.com and cannot do anything without it — including a dry run, which reads "
            "the repo to work out what would change. See SETUP.md step 6."
        )

    # --- structural fixes. Idempotent, cheap, and checked every run rather than queued: all three
    # are comparisons against the live repo, so "is there anything to do" is the same call as
    # doing it. `indexnow.ensure_key` is one read after the first run.
    for label, fn in (("delist", delist.apply), ("relink", publisher.relink),
                      ("indexnow-key", indexnow.ensure_key)):
        try:
            if url := fn(dry_run=dry_run):
                result.shipped.append(label)
                result.commits.append(url)
                print(f"  {label}: {url}")
        except Exception as exc:  # noqa: BLE001 — one failing fix must not block the others
            log.error("%s failed: %s", label, exc)
            result.failed.append(f"{label}: {str(exc)[:120]}")

    # --- composed items
    posts_today = ledger.shipped_today(ledger.Kind.POST)

    for item in ledger.planned():
        if _stale(item):
            print(f"  dropped #{item.id} ({item.kind}) — composed {STALE_DAYS}+ days ago")
            if not dry_run:
                ledger.drop(item.id, f"stale: composed at {item.created}")
            result.dropped.append(f"#{item.id} {item.title}")
            continue

        if item.kind == ledger.Kind.POST and posts_today >= policy.MAX_POSTS_PER_DAY:
            print(f"  holding #{item.id} — {posts_today} post(s) already published today")
            continue

        try:
            if item.kind == ledger.Kind.POST:
                meta = item.meta
                draft = Draft(
                    slug=meta.get("slug", ""), title=item.title,
                    description=meta.get("description", ""),
                    markdown=meta.get("markdown", item.body),
                    target_query=meta.get("query", ""), evidence=meta.get("evidence", ""),
                    # The gate ran at compose time and blocked anything that failed it; this record
                    # is what it decided, not a second opinion.
                    verdict=qgate.Verdict(passed=True),
                )
                url = publisher.publish(draft, dry_run=dry_run)
                posts_today += 1

            elif item.kind == ledger.Kind.META:
                url = publisher.retitle(
                    item.meta["slug"], item.meta["title"], item.meta["description"],
                    rationale=item.rationale, dry_run=dry_run,
                )

            elif item.kind == ledger.Kind.FAQ:
                url = publisher.append_section(
                    item.meta["slug"], item.meta["block"], rationale=item.rationale,
                    dry_run=dry_run,
                )

            elif item.kind == ledger.Kind.PRUNE:
                # Both halves, in one item: the header stops it being indexed, and dropping it from
                # the sitemap stops us asking for it to be. Doing only the first leaves the site
                # submitting URLs it has told Google not to index.
                url = delist.apply_noindex(item.meta["paths"], item.rationale, dry_run=dry_run)
                sitemap.drop_urls(set(item.meta["urls"]), item.rationale, dry_run=dry_run)

            elif item.kind == ledger.Kind.SITEMAP:
                url = sitemap.drop_urls(set(item.meta["urls"]), item.rationale, dry_run=dry_run)

            else:
                log.warning("#%s has unknown kind %r — dropping", item.id, item.kind)
                if not dry_run:
                    ledger.drop(item.id, f"unknown kind {item.kind}")
                result.dropped.append(f"#{item.id} {item.kind}")
                continue

        except Exception as exc:  # noqa: BLE001 — record it against the item and keep going
            log.error("%s #%s failed: %s", item.kind, item.id, exc)
            if not dry_run:
                ledger.fail(item.id, str(exc))
            result.failed.append(f"#{item.id} {item.title}: {str(exc)[:120]}")
            continue

        result.urls |= _urls_for(item)
        if dry_run:
            continue
        ledger.ship(item.id, url)
        result.shipped.append(f"#{item.id} {item.title}")
        if url:
            result.commits.append(url)
            print(f"  {item.kind}: {url}")

    # Google has no submission API for ordinary pages, so for Google the mechanism is the sitemap
    # and the internal links, both regenerated in the commits above. This covers everyone else:
    # one POST reaches Bing, Yandex, Seznam and Naver, and Bing's index is what Copilot reads.
    if result.urls and (result.commits or dry_run):
        result.submitted = indexnow.submit(result.urls, dry_run=dry_run)
        if result.submitted:
            print(f"  indexnow: submitted {result.submitted} URL(s)")

    return result


def _urls_for(item: ledger.Item) -> set[str]:
    """Which live URLs an applied item changed."""
    from autoseo.core.config import settings

    # A page being switched off must not be submitted for recrawl in the same breath. IndexNow is
    # for "come and look at this"; these are the pages we have just told Google to forget.
    if item.kind in (ledger.Kind.PRUNE, ledger.Kind.SITEMAP):
        return set()

    slug = item.meta.get("slug")
    if not slug:
        return set()
    urls = {f"{settings.site}/blog/{slug}"}
    if item.kind == ledger.Kind.POST:
        # The listing page changed too — it gained an entry, and it is how a crawler reaches the
        # new page from anywhere else on the site.
        urls.add(f"{settings.site}/blog")
    return urls
