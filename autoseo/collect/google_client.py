"""Shared Google Search Console client.

Read-only by construction: the only scope requested is webmasters.readonly, which is
sufficient for both Search Analytics and URL Inspection.
"""

from __future__ import annotations

from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

from autoseo.core.config import ConfigError, settings
from autoseo.core.log import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


@lru_cache(maxsize=1)
def search_console() -> Resource:
    creds = service_account.Credentials.from_service_account_info(
        settings.google_credentials_info(), scopes=SCOPES
    )
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


@lru_cache(maxsize=1)
def resolve_site_url() -> str:
    """Figure out the Search Console property identifier.

    Domain properties look like 'sc-domain:getdailyvox.com'; URL-prefix properties look like
    'https://getdailyvox.com/'. Rather than make the operator guess, ask the API which
    properties this service account can actually see.
    """
    if settings.gsc_site_url:
        return settings.gsc_site_url

    service = search_console()
    entries = service.sites().list().execute().get("siteEntry", [])
    if not entries:
        raise ConfigError(
            "The service account can't see any Search Console properties.\n"
            "  Add its client_email as an OWNER on the getdailyvox.com property:\n"
            "  Search Console -> Settings -> Users and permissions -> Add user\n"
            "  (URL Inspection needs owner-level access; Full is not enough.)"
        )

    host = settings.site.split("//", 1)[-1].strip("/")
    ranked = sorted(
        entries,
        key=lambda e: (
            0 if e["siteUrl"] == f"sc-domain:{host}" else
            1 if host in e["siteUrl"] else 2,
            e["siteUrl"],
        ),
    )
    best = ranked[0]
    if host not in best["siteUrl"]:
        raise ConfigError(
            f"No Search Console property matches {host}. "
            f"Visible properties: {[e['siteUrl'] for e in entries]}"
        )
    log.info("Using Search Console property %s (%s)", best["siteUrl"], best.get("permissionLevel"))
    return best["siteUrl"]
