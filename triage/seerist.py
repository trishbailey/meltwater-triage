"""Parse Seerist "Search Result Report" PDF exports into alert rows.

Each item in the report reads:
    Nov 05, 2026 · News & Social          <- date and type
    1                                      <- item number
    Short title ending in "..."            <- one to three lines
    Full text (often repeated)             <- summary
    https://example.com/post               <- source URL, may wrap onto a second line
    Countries: ... · Sentiment: ... · Author: ...
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone

_ITEM_HEAD = re.compile(r"^([A-Z][a-z]{2} \d{1,2}, \d{4})\s*·\s*(.+)$")
_NUMBER = re.compile(r"^\d{1,4}$")
_META = re.compile(r"^(Countries|Regions|Locations?|Topics|Sentiment|Author|Source|Categories)\s*:")
_URL = re.compile(r"^https?://\S+$")
_NOISE = (re.compile(r"^Search Result Report\b"), re.compile(r"^©\s*\d{4} Seerist"),
          re.compile(r"^Showing \d+ of \d+ results"), re.compile(r"^Search Results \(\d+\)"),
          re.compile(r"^SEERIST$"))
SUMMARY_MAX = 1200


class SeeristError(ValueError):
    pass


def pdf_lines(data: bytes) -> list[str]:
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:
        raise SeeristError("Could not read the PDF. Upload a Seerist PDF export.") from exc
    return [ln.strip() for ln in text.splitlines()]


def _meta_fields(meta: str) -> dict:
    out = {}
    for part in re.split(r"\s+·\s+", meta):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _dedupe_repeat(text: str) -> str:
    """Seerist prints the full text more than once; keep the first copy."""
    probe = text[:40]
    if len(probe) >= 20:
        nxt = text.find(probe, 1)
        if nxt > 0:
            return text[:nxt].strip()
    return text


def _split_title(joined: str) -> tuple[str, str]:
    """Title is the truncated headline printed before the full text starts."""
    probe = joined[:30]
    nxt = joined.find(probe, 1) if len(probe) >= 15 else -1
    if nxt > 0:
        title, rest = joined[:nxt].strip(), joined[nxt:].strip()
    else:
        cut = joined.find("...")
        cut = cut if 0 < cut < 300 else joined.find("…")
        if 0 < cut < 300:
            title, rest = joined[:cut].strip(), joined[cut + 3:].strip()
        else:
            title, rest = joined[:160].strip(), joined
    title = title.rstrip(".…").strip()
    summary = _dedupe_repeat(rest) or title
    return title, summary


def _parse_item(date_s: str, kind: str, body: list[str]) -> dict | None:
    if body and _NUMBER.match(body[0]):
        body = body[1:]
    meta_at = next((i for i, ln in enumerate(body) if _META.match(ln)), len(body))
    content, meta = body[:meta_at], " ".join(body[meta_at:])
    url, text_lines, i = "", [], 0
    while i < len(content):
        ln = content[i]
        if _URL.match(ln) and not url:
            url = ln
            # A long URL wraps onto lines with no spaces right before the metadata.
            while i + 1 < len(content) and " " not in content[i + 1] and not _URL.match(content[i + 1]):
                url += content[i + 1]
                i += 1
        else:
            text_lines.append(ln)
        i += 1
    if not text_lines and not url:
        return None
    title, summary = _split_title(" ".join(text_lines).strip())
    fields = _meta_fields(meta)
    location = fields.get("countries") or fields.get("locations") or fields.get("location") or fields.get("regions") or ""
    try:
        posted = datetime.strptime(date_s, "%b %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        posted = None
    return {"title": title[:500], "date": date_s, "posted_at": posted, "type": kind.strip(),
            "location": location, "summary": summary[:SUMMARY_MAX], "url": url,
            "author": fields.get("author", ""), "sentiment": fields.get("sentiment", ""),
            "topics": fields.get("topics", "")}


def parse_lines(lines: list[str]) -> list[dict]:
    items, cur = [], None
    for ln in lines:
        if not ln or any(p.match(ln) for p in _NOISE):
            continue
        head = _ITEM_HEAD.match(ln)
        if head:
            if cur:
                items.append(cur)
            cur = (head.group(1), head.group(2), [])
            continue
        if cur:
            cur[2].append(ln)
    if cur:
        items.append(cur)
    out = [_parse_item(d, k, b) for d, k, b in items]
    return [it for it in out if it]


def parse_pdf(data: bytes) -> list[dict]:
    if not data.startswith(b"%PDF"):
        raise SeeristError("That file is not a PDF. Upload a Seerist PDF export.")
    return parse_lines(pdf_lines(data))


def item_key(item: dict) -> str | None:
    """Dedupe key for items with no URL: hash of title plus date."""
    if item.get("url"):
        return None
    raw = f"{item.get('title', '').strip().lower()}|{item.get('date', '')}"
    return "seerist:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def to_alert(item: dict) -> dict:
    sentiment = (item.get("sentiment") or "").lower()
    terms = [t for t in (item.get("type"), item.get("location") and f"Location: {item['location']}",
                         sentiment and f"Sentiment: {item['sentiment']}") if t]
    text = item.get("title", "")
    if item.get("summary") and item["summary"] != text:
        text = f"{text}\n\n{item['summary']}" if text else item["summary"]
    return {"source": "seerist", "url": item.get("url") or "", "author": item.get("author") or None,
            "text": text[:5000] or None, "posted_at": item.get("posted_at"),
            "matched_terms": "; ".join(terms),
            "signal_level": "medium" if sentiment == "negative" else "low"}
