"""301 one page onto another, permanently.

The only operation this system performs that a reader can notice going wrong. Everything else adds
a page, edits a title, or hides something nobody was reading; this takes a URL that works today and
makes it stop existing. So it is narrow by construction: one redirect per commit, written into the
same `vercel.json` the noindex headers live in, with the source path checked against the site's own
sitemap before anything is sent.

Vercel evaluates `redirects` before serving static files, so the rendered HTML can stay in the repo.
That matters — undoing this is deleting one JSON object, and the page it pointed at is still there.

301 rather than Vercel's default 308. Both are permanent and Google treats them identically, but 301
is the status every other tool, log parser and human on earth recognises without looking it up.
"""

from __future__ import annotations

import json

from autoseo.core.log import get_logger
from autoseo.publish import site

log = get_logger(__name__)

VERCEL = f"{site.SITE_DIR}/vercel.json"


def existing(config: dict) -> set[str]:
    return {r.get("source") for r in config.get("redirects", []) if isinstance(r, dict)}


def add(source: str, destination: str, rationale: str, dry_run: bool = False) -> str:
    """Redirect `source` to `destination`. Idempotent; refuses to build a chain or a loop."""
    if not source.startswith("/") or not destination.startswith("/"):
        raise ValueError(f"redirects take absolute paths, got {source!r} -> {destination!r}")
    if source == destination:
        raise ValueError("refusing to redirect a page to itself")

    raw = site.read_text(VERCEL)
    if raw is None:
        raise RuntimeError(f"{VERCEL} not found in {site.SITE_REPO}")
    config = json.loads(raw)
    redirects = config.setdefault("redirects", [])
    sources = existing(config)

    if source in sources:
        log.info("%s already redirects", source)
        return ""

    # A redirect onto something that is itself redirected sends the reader through two hops and
    # dilutes the signal the merge exists to consolidate. Point at the final destination instead.
    for rule in redirects:
        if isinstance(rule, dict) and rule.get("source") == destination:
            raise RuntimeError(
                f"{destination} is itself redirected to {rule.get('destination')} — "
                f"redirect {source} there instead of building a chain"
            )

    redirects.append({"source": source, "destination": destination, "statusCode": 301})
    return site.commit(
        {VERCEL: json.dumps(config, indent=2) + "\n"},
        f"seo: 301 {source} -> {destination}\n\n{rationale}",
        dry_run=dry_run,
    )
