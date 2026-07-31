"""Logging with secret redaction.

GitHub already masks registered secrets in Actions logs. This is the second line of defence,
and it also covers local runs where GitHub's masking does not exist.
"""

from __future__ import annotations

import logging
import os
import re
import sys

# Anything that looks like a credential, even if we never registered it as a secret.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),           # Google API keys
    re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),           # AI Studio keys
    re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}"),        # GitHub tokens
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),      # GitHub fine-grained PATs
    re.compile(r"sk-[0-9A-Za-z\-_]{20,}"),            # OpenAI / Anthropic style
    re.compile(r"IGA[0-9A-Za-z]{20,}"),               # Instagram tokens
    re.compile(r"\d{8,10}:AA[0-9A-Za-z_\-]{30,}"),    # Telegram bot tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r'"private_key"\s*:\s*"[^"]+"'),       # service-account JSON
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_RedactingFormatter("%(asctime)s  %(levelname)-7s %(name)s  %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("AUTOSEO_LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger
