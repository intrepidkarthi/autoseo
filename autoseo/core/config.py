"""Typed settings, read identically from a local .env or from CI env vars.

There is deliberately no separate "CI config" path — if it works locally it works in Actions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Does not override anything already in the environment,
    so CI (where vars are already set) is unaffected."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when a credential a command actually needs is missing."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    site: str = field(default_factory=lambda: _env("AUTOSEO_SITE", "https://getdailyvox.com"))
    gsc_site_url: str = field(default_factory=lambda: _env("AUTOSEO_GSC_SITE_URL"))
    db_path: Path = field(
        default_factory=lambda: REPO_ROOT / _env("AUTOSEO_DB", "state/autoseo.db")
    )
    inspect_limit: int = field(
        default_factory=lambda: int(_env("AUTOSEO_INSPECT_LIMIT", "300") or 300)
    )
    monthly_cap_usd: Decimal = field(
        default_factory=lambda: Decimal(_env("AUTOSEO_MONTHLY_CAP_USD", "5.00") or "5.00")
    )

    # --- credentials ---------------------------------------------------------
    gsc_service_account_json: str = field(default_factory=lambda: _env("GSC_SERVICE_ACCOUNT_JSON"))
    bing_api_key: str = field(default_factory=lambda: _env("BING_WEBMASTER_API_KEY"))
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))
    gh_dailyvox_token: str = field(default_factory=lambda: _env("GH_DAILYVOX_TOKEN"))
    yt_token_json: str = field(default_factory=lambda: _env("YT_TOKEN_JSON"))

    @property
    def state_dir(self) -> Path:
        return self.db_path.parent

    def google_credentials_info(self) -> dict:
        """Parse the service-account JSON, with an error that says what to fix."""
        if not self.gsc_service_account_json:
            raise ConfigError(
                "GSC_SERVICE_ACCOUNT_JSON is not set.\n"
                "  Local: add it to .env (see .env.example)\n"
                "  CI:    GitHub -> autoseo -> Settings -> Environments -> read"
            )
        try:
            return json.loads(self.gsc_service_account_json)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                "GSC_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the *entire* downloaded "
                "service-account file, including the outer braces."
            ) from exc

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n, "")]
        if missing:
            raise ConfigError(
                "Missing required setting(s): "
                + ", ".join(sorted(missing))
                + "\nSee SETUP.md for how to create each one."
            )


settings = Settings()
