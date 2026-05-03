from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone


# Keys whose JSON-/dict-like values must never appear in logs (broker SDK & HTTP traces).
_REDACT_KEYS: tuple[str, ...] = (
    "password",
    "totp",
    "otp",
    "pin",
    "refresh_token",
    "refreshToken",
    "access_token",
    "accessToken",
    "authToken",
    "jwtToken",
    "feedToken",
    "api_key",
    "apiKey",
    "client_secret",
    "clientSecret",
    "secret",
    "X-PrivateKey",
    "x-private-key",
    "Authorization",
)

# Long bearer-style secrets in plain text
_RE_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._+-]{8,}\b")


def redact_credentials(text: str) -> str:
    """Remove broker passwords, TOTP, tokens, and API key material from log text."""
    if not text:
        return text
    out = text
    for key in _REDACT_KEYS:
        pat = re.compile(
            rf"(?i)(['\"])({re.escape(key)})\1\s*:\s*(['\"])(?:[^'\"\\]|\\.)*\3"
        )
        out = pat.sub(r"\1\2\1: \3***\3", out)
        # Unquoted header style: X-PrivateKey: abc123
        pat2 = re.compile(rf"(?i)(\b{re.escape(key)}\s*[:=]\s*)(\S+)")
        out = pat2.sub(r"\1***", out)
    out = _RE_BEARER.sub("Bearer ***", out)
    return out


class CredentialRedactionFilter(logging.Filter):
    """Strips secrets from log records before handlers format them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        redacted = redact_credentials(msg)
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


_redaction_filter_singleton: CredentialRedactionFilter | None = None


def _get_redaction_filter() -> CredentialRedactionFilter:
    global _redaction_filter_singleton
    if _redaction_filter_singleton is None:
        _redaction_filter_singleton = CredentialRedactionFilter()
    return _redaction_filter_singleton


def _logger_has_filter(log: logging.Logger) -> bool:
    return any(type(f) is CredentialRedactionFilter for f in log.filters)


def _handler_has_filter(handler: logging.Handler) -> bool:
    return any(type(f) is CredentialRedactionFilter for f in handler.filters)


def attach_redaction_filters_deep() -> None:
    """
    Attach credential redaction so broker SDK loggers/handlers cannot leak secrets.
    Safe to call multiple times (e.g. once at startup and again after SmartApi import).
    """
    filt = _get_redaction_filter()
    visited_handlers: set[int] = set()

    def touch_logger(log: logging.Logger) -> None:
        if not _logger_has_filter(log):
            log.addFilter(filt)
        for h in log.handlers:
            hid = id(h)
            if hid in visited_handlers:
                continue
            visited_handlers.add(hid)
            if not _handler_has_filter(h):
                h.addFilter(filt)

    touch_logger(logging.getLogger())
    for name in ("smartConnect", "smartWebSocketV2", "SmartApi"):
        touch_logger(logging.getLogger(name))

    manager = logging.root.manager
    for log in list(manager.loggerDict.values()):
        if isinstance(log, logging.Logger):
            touch_logger(log)


def setup_logging() -> None:
    # Use fixed IST offset so logging works even when tzdata is unavailable.
    ist = timezone(timedelta(hours=5, minutes=30), name="IST")
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )
    formatter.converter = lambda ts: datetime.fromtimestamp(ts, tz=ist).timetuple()  # type: ignore[assignment]

    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.StreamHandler()],
        force=True,
    )
    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)

    attach_redaction_filters_deep()
