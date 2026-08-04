"""One-time YouTube OAuth, run locally. Ported from relay so nothing lives outside this repo.

You run this once on your laptop. It opens a browser, you consent, and it prints the JSON to paste
into the `publishing` GitHub environment as YT_TOKEN_JSON. CI never runs this — a browser consent
flow cannot happen on a runner, which is why the refresh token has to be minted here and carried
across.

The trap this checks for: an OAuth consent screen left in *Testing* issues refresh tokens that
expire after **7 days**. Everything works, then silently stops a week later — the worst failure
shape there is, because it looks like success. The app may remain *unverified*; unverified and
in-production are different things, and only the second one matters here.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoseo.core.log import get_logger

log = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
DEFAULT_SECRET = Path("client_secret.json")
DEFAULT_TOKEN = Path("token.json")


def authorise(client_secret: Path = DEFAULT_SECRET, token_out: Path = DEFAULT_TOKEN) -> str:
    """Run the consent flow and return the token JSON."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "google-auth-oauthlib is needed for the one-time auth flow:\n"
            "  pip install google-auth-oauthlib"
        ) from exc

    if not client_secret.exists():
        raise RuntimeError(
            f"{client_secret} not found.\n\n"
            "Google Cloud Console -> APIs & Services -> Credentials -> Create credentials\n"
            "  -> OAuth client ID -> Application type: Desktop app -> Download JSON\n"
            f"Save it as {client_secret} and run this again."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    payload = creds.to_json()
    token_out.write_text(payload)

    data = json.loads(payload)
    if not data.get("refresh_token"):
        raise RuntimeError(
            "No refresh token was issued. That happens when the account has already granted "
            "consent — revoke it at https://myaccount.google.com/permissions and run this again."
        )

    print(f"\n  Token written to {token_out}\n")
    print("  Paste the ENTIRE contents into GitHub:")
    print("    autoseo -> Settings -> Environments -> publishing -> Add secret")
    print("    Name: YT_TOKEN_JSON\n")
    print("  Check the consent screen shows 'In production' before relying on this.")
    print("  In Testing, the refresh token expires after 7 days and uploads stop silently.\n")
    return payload


def describe_token(token: Path = DEFAULT_TOKEN) -> None:
    """Report what a token file contains, without printing anything secret."""
    if not token.exists():
        print(f"  {token} does not exist yet — run `autoseo youtube-auth` first.")
        return
    data = json.loads(token.read_text())
    print(f"  refresh_token present : {bool(data.get('refresh_token'))}")
    print(f"  scopes                : {', '.join(data.get('scopes', []))}")
    print(f"  expiry (access token) : {data.get('expiry', 'unknown')}")
    print("  The access token expiring is normal; the refresh token is what must persist.")
