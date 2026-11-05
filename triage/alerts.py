"""Validation for early-warning alerts posted to /api/alerts."""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

SOURCES = ("x", "bluesky", "facebook", "instagram", "newsletter", "seerist")
SIGNAL_LEVELS = ("critical", "high", "medium", "low")  # most to least severe
MAX_TEXT = 5000


_HOST_ALIASES = {"twitter.com": "x.com", "mobile.twitter.com": "x.com", "m.facebook.com": "facebook.com",
                 "bsky.app": "bsky.app"}


def normalize_url(url: str) -> str:
    """Lowercase host without www., same-site aliases merged, no query/fragment/trailing slash."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    host = _HOST_ALIASES.get(host, host)
    path = parts.path.rstrip("/")
    if host == "facebook.com" or host.endswith("youtube.com"):
        q = parts.query  # these sites carry the post id in the query string
        return f"{host}{path}?{q}" if q else f"{host}{path}"
    return f"{host}{path}"


class AlertError(ValueError):
    pass


def _str(payload: dict, key: str, limit: int, required: bool = False) -> str | None:
    v = payload.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise AlertError(f"'{key}' is required")
        return None
    if not isinstance(v, str):
        raise AlertError(f"'{key}' must be a string")
    return v.strip()[:limit]


def parse_alert(payload) -> dict:
    if not isinstance(payload, dict):
        raise AlertError("Body must be a JSON object")
    source = (_str(payload, "source", 16, True) or "").lower()
    if source not in SOURCES:
        raise AlertError("'source' must be one of: " + ", ".join(SOURCES))
    url = _str(payload, "url", 2000, True)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise AlertError("'url' must be an http(s) URL")
    level = (_str(payload, "signal_level", 16) or _str(payload, "signal", 16) or "").lower()
    if level not in SIGNAL_LEVELS:
        raise AlertError("'signal_level' must be one of: " + ", ".join(SIGNAL_LEVELS))
    terms = payload.get("matched_terms") or []
    if isinstance(terms, str):
        terms = [t for t in terms.split(";")]
    if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
        raise AlertError("'matched_terms' must be a list of strings")
    terms = [t.strip()[:100] for t in terms if t.strip()][:50]
    posted = _str(payload, "posted_at", 64)
    posted_at = None
    if posted:
        try:
            posted_at = datetime.fromisoformat(posted.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlertError("'posted_at' must be an ISO 8601 timestamp") from exc
        if posted_at.tzinfo is None:
            posted_at = posted_at.replace(tzinfo=timezone.utc)
    return {"source": source, "url": url, "author": _str(payload, "author", 300),
            "text": _str(payload, "text", MAX_TEXT), "posted_at": posted_at,
            "matched_terms": "; ".join(terms), "signal_level": level}
