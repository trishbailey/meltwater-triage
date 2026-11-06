"""Collapse exact duplicate text and read only a real stored source link.

Exact serialization (the same text twice) and a repeated identical mention
prefix are one post. A reply that adds different text, and the same wording
posted at two different URLs, are kept.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

_WS = re.compile(r"\s+")
_MENTION = re.compile(r"(@[\w.]+)(?:\s+\1\b)+")
_BAD = {
    "", "none", "null", "nan", "n/a", "na", "unknown", "unknown source",
    "-", "undefined", "nil", "anonymous",
}
_TEXT_FIELDS = (
    "hit sentence", "opening text", "full text", "content", "text",
    "post text", "message", "tweet", "comment", "title",
)
_HANDLE_FIELDS = ("author handle", "author", "username", "user name")
_NAME_FIELDS = ("author name", "name")
_DATE_FIELDS = ("post_date", "date", "published", "published_at", "posted_at", "timestamp")
_URL_FIELDS = ("url", "link", "original url", "source url", "permalink")
_RESERVED = {
    "i", "home", "share", "search", "explore", "intent", "hashtag", "messages",
    "notifications", "settings", "compose", "watch", "playlist", "shorts",
    "feed", "events", "photo", "stories", "reel", "p", "status", "channel",
}


def _missing(value) -> bool:
    if value is None:
        return True
    try:
        import pandas as pd
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _text(value) -> str:
    if _missing(value):
        return ""
    text = str(value).strip()
    if text.lower() in _BAD:
        return ""
    return text


def _cell(row, name: str):
    if row is None:
        return None
    if isinstance(row, dict):
        items = row.items()
    elif hasattr(row, "items"):
        items = row.items()
    else:
        return None
    for key, val in items:
        if str(key).strip().lower() == name:
            return None if _missing(val) else val
    return None


def collapse_mentions(text: str) -> str:
    """Collapse a repeated identical @mention. Different handles stay."""
    prev = None
    out = text or ""
    while prev != out:
        prev = out
        out = _MENTION.sub(r"\1", out)
    return out


def collapse_exact_repeat(text: str) -> str:
    """Return one copy when the text is the same unit repeated two or three times."""
    raw = (text or "").strip()
    if len(raw) < 16:
        return raw
    parts = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    if len(parts) in (2, 3):
        norms = [_WS.sub(" ", p).strip().lower() for p in parts]
        if len(set(norms)) == 1 and len(norms[0]) >= 8:
            return parts[0]
    norm = _WS.sub(" ", raw).strip()
    for copies in (2, 3):
        sep = copies - 1
        if (len(norm) - sep) % copies:
            continue
        unit_len = (len(norm) - sep) // copies
        if unit_len < 8:
            continue
        unit = norm[:unit_len]
        if norm == " ".join([unit] * copies):
            return unit
    return raw


def collapse_exact(text: str) -> str:
    """Normalize mention-prefix repeats and exact doubled text. Other wording stays."""
    return collapse_exact_repeat(collapse_mentions(text or ""))


def join_unique(parts: list[str]) -> str:
    """Keep distinct pieces. Drop a piece that is exactly contained in a longer one."""
    kept: list[str] = []
    norms: list[str] = []
    for part in parts:
        raw = collapse_exact(part).strip()
        norm = _WS.sub(" ", raw).strip().lower()
        if not norm:
            continue
        if any(norm == have or norm in have for have in norms):
            continue
        next_kept, next_norms = [], []
        for have, stored in zip(norms, kept):
            if have != norm and have in norm:
                continue
            next_kept.append(stored)
            next_norms.append(have)
        kept, norms = next_kept, next_norms
        kept.append(raw)
        norms.append(norm)
    return "\n\n".join(kept)


def single_post_text(row, columns=None) -> str:
    """One original body. Exact copies across columns are not repeated."""
    cells: list[str] = []
    if columns is not None:
        for col in columns:
            if isinstance(row, dict):
                val = row.get(col)
            else:
                try:
                    val = row[col]
                except Exception:
                    val = _cell(row, str(col).strip().lower())
            cells.append(_text(val))
    else:
        for name in _TEXT_FIELDS:
            cells.append(_text(_cell(row, name)))
    return join_unique([c for c in cells if c])


def usable_source_url(value) -> str:
    """A stored http(s) permalink, or a bare known-host URL with a scheme added.

    A place name, an empty cell, or any other non-URL is omitted. Nothing is guessed.
    """
    text = _text(value)
    if not text or " " in text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    bare = re.match(
        r"^(?:www\.)?(?:x\.com|twitter\.com|bsky\.app|youtube\.com|youtu\.be|"
        r"facebook\.com|instagram\.com|m\.facebook\.com)/",
        text, re.I,
    )
    if not re.match(r"https?://", text, re.I):
        if not bare:
            return ""
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return text


def first_source_url(row) -> str:
    for name in _URL_FIELDS:
        url = usable_source_url(_cell(row, name))
        if url:
            return url
    return ""


def supplied_handle(value) -> str:
    text = _text(value)
    if not text:
        return ""
    return text


def handle_from_url(url: str) -> str:
    """Username embedded in a stored post URL. Channel ids and /watch links are not names."""
    url = usable_source_url(url)
    if not url:
        return ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in parts.path.split("/") if s]
    if host in {"x.com", "twitter.com", "mobile.twitter.com"} and segs:
        handle = segs[0]
        if handle.lower() not in _RESERVED and not handle.isdigit():
            return "@" + handle.lstrip("@")
    if host == "bsky.app" and len(segs) >= 2 and segs[0] == "profile" and segs[1].lower() not in _RESERVED:
        return "@" + segs[1]
    if host.endswith("youtube.com") or host == "youtu.be":
        if segs and segs[0].startswith("@"):
            return segs[0]
        if len(segs) >= 2 and segs[0] in {"user", "c"} and segs[1].lower() not in _RESERVED:
            return "@" + segs[1]
    if host.endswith("facebook.com") and segs and segs[0].lower() not in _RESERVED:
        return segs[0]
    return ""


def display_handle(row) -> str:
    for name in _HANDLE_FIELDS:
        handle = supplied_handle(_cell(row, name))
        if handle:
            return handle if handle.startswith("@") or " " in handle else handle
    for name in _NAME_FIELDS:
        name_val = supplied_handle(_cell(row, name))
        if name_val:
            return name_val
    return handle_from_url(first_source_url(row))


def _parse_when(value):
    if _missing(value):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text or text.lower() in _BAD:
            return None
        try:
            import pandas as pd
            ts = pd.to_datetime(text, errors="coerce", utc=True)
            if ts is None or pd.isna(ts):
                return None
            dt = ts.to_pydatetime()
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def row_datetime(row):
    for name in _DATE_FIELDS:
        dt = _parse_when(_cell(row, name))
        if dt is not None:
            return dt
    return None


def format_when(value) -> str:
    """A stored timestamp. Date-only values stay date-only. Missing stays blank."""
    dt = value if isinstance(value, datetime) else _parse_when(value)
    if dt is None:
        return ""
    stamp = dt.strftime("%-d %b %Y") if hasattr(dt, "strftime") else ""
    # %-d is not portable on every libc; fall back.
    try:
        day = dt.strftime("%-d")
    except ValueError:
        day = str(dt.day)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    date = f"{day} {months[dt.month - 1]} {dt.year}"
    if dt.hour or dt.minute or dt.second:
        return f"{date}, {dt.strftime('%H:%M')} UTC"
    return date


def format_row_when(row) -> str:
    dt = row_datetime(row)
    return format_when(dt) if dt is not None else ""


def _url_key(url: str) -> str:
    url = usable_source_url(url)
    if not url:
        return ""
    try:
        from .alerts import normalize_url
        return normalize_url(url)
    except Exception:
        return url.lower()


def storage_dedupe_key(row) -> tuple | None:
    """Identity for one actual post. Independent cross-posts (different URLs) stay apart.

    Short or empty text is not a key, so rows that only share a placeholder link
    are not merged.
    """
    text = _WS.sub(" ", single_post_text(row)).strip().lower()
    if len(text) < 20:
        return None
    url = ""
    for name in _URL_FIELDS:
        url = _url_key(_text(_cell(row, name)))
        if url:
            break
    if url:
        return ("url", url, text)
    author = display_handle(row).strip().lower()
    if not author:
        return None
    when = row_datetime(row)
    day = when.strftime("%Y-%m-%d") if when is not None else ""
    return ("author", author, text, day)


def dedupe_posts(df):
    """Keep the first row of an exact duplicate. Different URLs are kept."""
    if df is None or getattr(df, "empty", True):
        return df
    seen = set()
    keep = []
    for i, row in df.iterrows():
        rec = row.to_dict()
        key = storage_dedupe_key(rec)
        doc = str(_cell(rec, "document id") or "")
        ident = key if key is not None else ("row", doc, i)
        if ident in seen:
            continue
        seen.add(ident)
        keep.append(i)
    return df.loc[keep].copy()
