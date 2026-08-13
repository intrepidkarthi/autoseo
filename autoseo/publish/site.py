"""Write to the site repo directly, through the git data API.

Autonomy removed the pull request. A PR was never a safety mechanism here — it was a place for a
human to look, and there is no human in the loop any more. What replaces it is upstream: the
quality gate blocks the draft, the cadence policy blocks the volume, and every action is recorded in
the ledger and committed to this repo's history before it ships. If something reads wrong on the
site, the fix is `git revert` on one commit, which is both faster and more complete than closing a
PR would have been.

One commit per action, built through the git data API rather than the contents API. The contents
API writes one file per call, so a post — markdown, rendered HTML, sitemap, index — landed as four
separate commits, and any failure between them left the site in a state no single revert undoes.
A tree commit is atomic: all four files or none.
"""

from __future__ import annotations

import base64

import httpx

from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

API = "https://api.github.com"
SITE_REPO = "intrepidkarthi/dailyvox"
SITE_DIR = "solyn/website"
CONTENT_DIR = f"{SITE_DIR}/content/articles"
BASE_BRANCH = "main"

# Files the bot is allowed to write, as path prefixes. Every commit is checked against this before
# it is sent. The autonomous loop composes its own diffs, so the blast radius has to be bounded by
# something other than review — a bug that produced a path outside these prefixes would otherwise
# be free to overwrite the site's source.
WRITABLE = (
    f"{CONTENT_DIR}/",                    # article markdown
    f"{SITE_DIR}/public/blog/",           # rendered article pages and the index
    f"{SITE_DIR}/public/sitemap-",        # sitemap-blog.xml, -core.xml, -articles.xml
    f"{SITE_DIR}/vercel.json",            # noindex headers for the de-listed clusters
)

# The IndexNow key file has to sit at the site root, outside every prefix above. Allowed by exact
# name rather than by widening the root prefix to `public/`, which would put the homepage, the
# stylesheet and 1,500 templated pages inside the blast radius to permit one 32-byte file.
def _writable(path: str) -> bool:
    from autoseo.publish import indexnow
    return path.startswith(WRITABLE) or path == indexnow.key_file_path()


class NotWritable(RuntimeError):
    """A commit tried to touch a path outside WRITABLE."""


class AlreadyApplied(RuntimeError):
    """The change is already on the site, so there is nothing left to do.

    Distinct from a failure, and the distinction is load-bearing. A ledger and a site can drift —
    the first live run shipped five commits and then lost the record of them — and when they do, the
    next run re-attempts work that is already done. Treating "it is already there" as an error
    leaves the item stuck, and a stuck item counts against the caps and blocks the following cycle
    from planning anything at all. Re-running has to converge.
    """


def _headers() -> dict[str, str]:
    if not settings.gh_dailyvox_token:
        raise ConfigError(
            "GH_DAILYVOX_TOKEN is not set. Create a fine-grained PAT scoped to "
            f"{SITE_REPO} with Contents: read+write, then add it to the publishing "
            "environment. See SETUP.md step 6."
        )
    return {
        "authorization": f"Bearer {settings.gh_dailyvox_token}",
        "accept": "application/vnd.github+json",
        "user-agent": "autoseo",
    }


def get(path: str) -> dict | list:
    r = httpx.get(f"{API}{path}", headers=_headers(), timeout=60.0)
    r.raise_for_status()
    return r.json()


def post(path: str, body: dict) -> dict:
    r = httpx.post(f"{API}{path}", headers=_headers(), json=body, timeout=60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def patch(path: str, body: dict) -> dict:
    r = httpx.patch(f"{API}{path}", headers=_headers(), json=body, timeout=60.0)
    if r.status_code >= 400:
        raise RuntimeError(f"GitHub {r.status_code} on {path}: {r.text[:300]}")
    return r.json()


def read_text(repo_path: str, ref: str = BASE_BRANCH) -> str | None:
    """File contents, or None if it does not exist."""
    try:
        blob = get(f"/repos/{SITE_REPO}/contents/{repo_path}?ref={ref}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    if isinstance(blob, list):
        raise RuntimeError(f"{repo_path} is a directory, not a file")
    return base64.b64decode(blob["content"]).decode("utf-8")


def list_dir(repo_path: str, ref: str = BASE_BRANCH) -> list[dict]:
    entries = get(f"/repos/{SITE_REPO}/contents/{repo_path}?ref={ref}")
    return entries if isinstance(entries, list) else []


def exists(repo_path: str, ref: str = BASE_BRANCH) -> bool:
    try:
        return bool(get(f"/repos/{SITE_REPO}/contents/{repo_path}?ref={ref}"))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        raise


def head_sha(ref: str = BASE_BRANCH) -> str:
    return get(f"/repos/{SITE_REPO}/git/ref/heads/{ref}")["object"]["sha"]


def commit(files: dict[str, str], message: str, dry_run: bool = False) -> str:
    """Write every file in one atomic commit on main. Returns the commit URL, or "" for a no-op.

    Unchanged files are dropped before the tree is built, so a run that decides nothing new produces
    no commit at all rather than an empty one — which keeps the site history readable as a list of
    real changes.
    """
    outside = [p for p in files if not _writable(p)]
    if outside:
        raise NotWritable(
            f"refusing to commit outside the writable paths: {', '.join(sorted(outside))}"
        )

    changed = {p: c for p, c in files.items() if read_text(p) != c}
    if not changed:
        log.info("no change: %s", message)
        return ""

    if dry_run:
        print(f"  would commit to {SITE_REPO}@{BASE_BRANCH}: {message}")
        for p in sorted(changed):
            print(f"      {p}")
        return ""

    parent = head_sha()
    base_tree = get(f"/repos/{SITE_REPO}/git/commits/{parent}")["tree"]["sha"]
    tree = post(f"/repos/{SITE_REPO}/git/trees", {
        "base_tree": base_tree,
        "tree": [
            {"path": p, "mode": "100644", "type": "blob", "content": c}
            for p, c in sorted(changed.items())
        ],
    })
    created = post(f"/repos/{SITE_REPO}/git/commits", {
        "message": message,
        "tree": tree["sha"],
        "parents": [parent],
    })
    # Not forced: a non-fast-forward means someone else pushed while this ran, and the right answer
    # is to fail and retry on the next cycle rather than to overwrite their work.
    patch(f"/repos/{SITE_REPO}/git/refs/heads/{BASE_BRANCH}", {"sha": created["sha"]})

    url = f"https://github.com/{SITE_REPO}/commit/{created['sha']}"
    log.info("committed %d file(s): %s", len(changed), url)
    return url
