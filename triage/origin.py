"""Where a post was published. Never the monitoring tool that delivered the export."""
from __future__ import annotations

from urllib.parse import urlsplit

# Values that name the export product rather than the publishing platform.
_NOT_A_SOURCE = {
    "meltwater", "meltwater export", "mw", "n/a", "na", "none", "null", "nan", "unknown", "-",
}
# Broad Meltwater source-type labels. Prefer the URL's site when these are all we have.
_GENERIC = {
    "news", "online news", "online", "blog", "blogs", "social", "social media", "forum",
    "forums", "review", "reviews", "comment", "comments", "other", "microblog",
    "television", "tv", "radio", "print", "podcast",
}
_ALIASES = {
    "twitter": "X", "x": "X", "x (twitter)": "X",
    "facebook": "Facebook", "fb": "Facebook",
    "instagram": "Instagram", "ig": "Instagram",
    "bluesky": "Bluesky", "bsky": "Bluesky",
    "youtube": "YouTube", "reddit": "Reddit", "tiktok": "TikTok",
    "linkedin": "LinkedIn", "threads": "Threads",
    "newsletter": "Newsletter", "web": "Web", "rss": "Web",
}
# Longest suffix wins (youtube.com before a shorter host).
_HOSTS = (
    ("twitter.com", "X"),
    ("x.com", "X"),
    ("facebook.com", "Facebook"),
    ("fb.com", "Facebook"),
    ("instagram.com", "Instagram"),
    ("bsky.app", "Bluesky"),
    ("bluesky.social", "Bluesky"),
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("reddit.com", "Reddit"),
    ("tiktok.com", "TikTok"),
    ("linkedin.com", "LinkedIn"),
    ("threads.net", "Threads"),
    ("news.google.com", "Google News"),
)


def _missing(v) -> bool:
    if v is None:
        return True
    try:
        import pandas as pd
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _get(row: dict, name: str):
    for key, val in row.items():
        if str(key).strip().lower() == name:
            return None if _missing(val) else val
    return None


def host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def platform_from_host(host: str) -> str:
    host = (host or "").lower()
    if not host or "meltwater" in host:
        return ""
    for suffix, name in _HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return name
    return host


def _clean_label(text: str) -> str:
    text = " ".join(str(text).split())
    if not text:
        return ""
    low = text.lower().strip(".")
    if low in _NOT_A_SOURCE or "meltwater" in low:
        return ""
    return _ALIASES.get(low, text[:120])


def origin_label(row: dict | None) -> str:
    """Publishing platform or site. The string 'Meltwater' is never returned."""
    row = row or {}
    url = ""
    for name in ("url", "link", "original url", "source url", "permalink"):
        val = _get(row, name)
        if val:
            url = str(val).strip()
            break
    from_host = platform_from_host(host_of(url))
    for name in ("origin_source", "source name", "source", "publisher", "outlet", "site", "domain"):
        val = _get(row, name)
        if not val:
            continue
        low = " ".join(str(val).split()).lower()
        if low in _GENERIC:
            continue
        named = _clean_label(val)
        if named:
            return named
    if from_host:
        return from_host
    return "Unknown source"


def backfill_sources(storage) -> int:
    """Fill source_name from stored export fields or the URL when it is blank or 'Meltwater'.

    A source that is already a platform or outlet is kept. No rows are deleted.
    """
    from sqlalchemy import select

    from .storage import posts
    changed = 0
    with storage.engine.begin() as conn:
        rows = conn.execute(select(
            posts.c.document_id, posts.c.source_name, posts.c.link, posts.c.data)).all()
        for doc, source_name, link, data in rows:
            blob = dict(data) if isinstance(data, dict) else {}
            current = (source_name or "").strip()
            low = current.lower()
            needs = (not current) or ("meltwater" in low) or low == "unknown source"
            if link and not any(blob.get(k) for k in ("link", "URL", "url", "Link")):
                blob["link"] = link
            label = origin_label(blob) if needs else current
            if not label:
                label = "Unknown source"
            if current == label and blob.get("origin_source") == label:
                continue
            blob["origin_source"] = label
            conn.execute(posts.update().where(posts.c.document_id == doc).values(
                source_name=label, data=blob))
            changed += 1
    return changed
