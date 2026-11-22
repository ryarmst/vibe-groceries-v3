"""Logging with secret redaction that stays active at every level, including DEBUG."""

from __future__ import annotations

import logging
import re

_PATTERNS = [
    # JWTs (access/id tokens)
    (re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*"), "<redacted-jwt>"),
    # Header-style secrets
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-apikey)(\s*[:=]\s*)([^\s,;]+(?:\s+[^\s,;]+)?)"), r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[\w.\-~+/=]+"), "Bearer <redacted>"),
    # JSON / form fields
    (re.compile(r'(?i)("?(?:access_?token|refresh_?token|id_?token|password|token|code_verifier)"?\s*[:=]\s*"?)[^"&,\s}]+'), r"\1<redacted>"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        return True


class QuietHealthChecks(logging.Filter):
    """Keep container health checks (every minute) out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logs full URLs at INFO; keep it quieter and still redacted.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").addFilter(QuietHealthChecks())
