"""Structured logging with credential redaction."""

import logging
import re
import sys

from .config import get_settings

_SECRET_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+", re.I),
    re.compile(r"(refresh_token[\"'\s:=]+)[^&\s\"',}]+", re.I),
    re.compile(r"(client_secret[\"'\s:=]+)[^&\s\"',}]+", re.I),
    re.compile(r"(bot)\d{6,}:[A-Za-z0-9_-]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
]


class RedactingFilter(logging.Filter):
    """Last line of defence: even an accidental log of a full URL or payload
    must not leak a token into disk or a shipping pipeline."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(r"\1<redacted>", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(component: str) -> logging.Logger:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [" + component + "] %(name)s: %(message)s"
    ))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    return logging.getLogger(component)
