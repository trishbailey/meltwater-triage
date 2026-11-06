"""Assemble Leaflet-ready markers for the dashboard map."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import geo
from .export import PROTEST_CATS, THREAT_CATS
from .origin import origin_label

_DATE = re.compile(
    r"\b(?:(\d{4}-\d{2}-\d{2})|"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?)|"
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?(?:\s+\d{4})?))\b",
    re.I,
)


def _aware(ts):
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or pd.isna(ts):
        return None
    if not isinstance(ts, datetime):
        try:
            ts = pd.Timestamp(ts).to_pydatetime()
        except Exception:
            return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    m = _DATE.search(text)
    if not m:
        return None
    raw = next(g for g in m.groups() if g)
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%b %d", "%B %d", "%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
        try:
            dt = datetime.strptime(raw.replace(".", ""), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now(timezone.utc).year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _split_places(value) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    out = []
    for part in value.replace("|", ";").split(";"):
        part = part.strip()
        if part and part.lower() not in {"unknown", "n/a", "none", "australia"}:
            out.append(part)
    return out


def _places_from_alert(row: dict) -> list[str]:
    places = []
    for t in str(row.get("matched_terms") or "").split(";"):
        t = t.strip()
        if t.lower().startswith("location:"):
            places.append(t.split(":", 1)[1].strip())
    return places


def _s(value) -> str:
    """String for map text. pandas NaN is truthy, so it must not be sliced."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _places_in_text(text: str, limit: int = 2) -> list[str]:
    """Known Australian cities and defence sites named in the post text (used when analysis gave no location)."""
    low = (text or "").lower()
    names = list(geo.CITY_CENTROIDS) + [a[0] for a in geo.ASSETS]
    found = []
    for name in sorted(set(names), key=len, reverse=True):
        n = name.lower()
        if len(n) >= 4 and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low):
            if not any(n in f.lower() for f in found):
                found.append(name)
        if len(found) >= limit:
            break
    return found


def _color_for(kind: str) -> str:
    return geo.THREAT_MAP_COLOR if kind == "threat" else geo.EVENT_COLOR


def _lookup(rec: dict, *names: str):
    for name in names:
        for k, v in rec.items():
            if str(k).strip().lower() == name.lower() and _s(v):
                return v
    return None


def build_map(raw: pd.DataFrame | None, days: int | None = 30) -> dict:
    """Threat and protest markers from the unified frame. Nothing older than `days`."""
    from .export import is_rejected, post_text
    now = datetime.now(timezone.utc)
    cutoff = None if not days else now - timedelta(days=int(days))
    events: list[dict] = []
    threats: list[dict] = []
    seen: set[tuple] = set()
    stats = {"protests": 0, "protests_located": 0, "from_text": 0}
    geo.reset_nominatim_budget(10)

    if raw is not None and not raw.empty and "category" in raw.columns:
        for rec in raw.to_dict(orient="records"):
            if is_rejected(rec.get("is_genuine")):
                continue
            when = _aware(rec.get("post_date")) or _aware(_lookup(rec, "date"))
            if cutoff and when and when < cutoff:
                continue
            cat = str(rec.get("category") or "")
            link = _s(rec.get("link")) or _s(_lookup(rec, "url"))
            summary = _s(rec.get("narrative_summary")) or post_text(rec)
            source = _s(rec.get("origin_source")) or origin_label({**rec, "link": link})
            date = when.strftime("%Y-%m-%d") if when else ""
            doc = _s(rec.get("document_id")) or _s(rec.get("item_id")) or _s(rec.get("Document ID"))
            if cat in PROTEST_CATS:
                stats["protests"] += 1
                places = (_split_places(rec.get("protest_location"))
                          or _split_places(_lookup(rec, "location")))
                from_text = False
                if not places:
                    places = _places_in_text(post_text(rec) or summary)
                    from_text = bool(places)
                located = False
                for place in places:
                    hit = geo.geocode(place)
                    if not hit or not geo.in_australia(hit["lat"], hit["lon"]):
                        continue
                    key = ("protest", doc, place)
                    if key in seen:
                        continue
                    seen.add(key)
                    located = True
                    events.append({
                        "kind": "protest", "lat": hit["lat"], "lon": hit["lon"], "place": place,
                        "label": place, "date": date, "source": source, "url": link,
                        "color": geo.EVENT_COLOR, "text": _s(summary)[:180],
                        "event_date": _s(rec.get("event_date")),
                        "note": "place read from the post text" if from_text else ""})
                stats["protests_located"] += int(located)
                stats["from_text"] += int(located and from_text)
            if cat in THREAT_CATS:
                for place in _split_places(rec.get("targets"))[:3]:
                    hit = geo.geocode(place)
                    if not hit or not geo.in_australia(hit["lat"], hit["lon"]):
                        continue
                    key = ("threat", doc, place)
                    if key in seen:
                        continue
                    seen.add(key)
                    threats.append({
                        "kind": "threat", "lat": hit["lat"], "lon": hit["lon"], "place": place,
                        "label": place, "date": date, "source": source, "url": link,
                        "color": geo.THREAT_MAP_COLOR, "text": _s(summary)[:180],
                        "threat_type": _s(rec.get("threat_type"))})

    return {
        "assets": [dict(m) for m in geo.asset_markers()],
        "events": events,
        "threats": threats,
        "days": days,
        "stats": stats,
        "bounds": {"south": geo.AU_SOUTH, "north": geo.AU_NORTH,
                   "west": geo.AU_WEST, "east": geo.AU_EAST},
        "legend": [
            {"label": "Sites", "color": geo.ASSET_COLOR, "swatch": "site"},
            {"label": "Threats", "color": geo.THREAT_MAP_COLOR, "swatch": "fill"},
            {"label": "Protests", "color": geo.EVENT_COLOR, "swatch": "fill"},
        ],
    }
