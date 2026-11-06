"""Assemble Leaflet-ready markers for the dashboard map."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import geo
from .dashboard import PROTEST_CATS, SOURCE_LABELS, THREAT_CATS
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


def _short_label(name: str) -> str:
    aliases = {
        "Osborne Naval Shipyard": "Osborne",
        "Australian Submarine Agency HQ (Majura Park)": "ASA Majura",
        "BAE Systems Edinburgh": "BAE Edinburgh",
        "Naval Communication Station Harold Holt": "Harold Holt",
        "Perth Convention and Exhibition Centre": "Perth CEC",
        "US Embassy Canberra": "US Embassy",
        "US Consulate Sydney": "US Consulate SYD",
        "US Consulate Melbourne": "US Consulate MEL",
        "Sydney Town Hall": "Sydney Town Hall",
        "NSW Parliament": "NSW Parliament",
        "Russell Offices": "Russell Offices",
        "HMAS Stirling": "HMAS Stirling",
        "HMAS Kuttabul": "HMAS Kuttabul",
        "RAAF Tindal": "RAAF Tindal",
        "Pine Gap": "Pine Gap",
        "Henderson": "Henderson",
    }
    return aliases.get(name, name if len(name) <= 22 else name[:20] + "…")


def _event_kind(status: str, upcoming: bool, refined: str, text: str) -> str:
    blob = f"{status} {refined} {text}".lower()
    if "advoc" in blob or any(word in blob for word in ("submission", "petition", "public comment", "have your say")):
        return "advocacy"
    if upcoming:
        return "planned"
    return "past"


def _color_for(kind: str) -> str:
    if kind == "advocacy":
        return geo.ADVOCACY_COLOR
    if kind == "planned":
        return geo.UPCOMING_COLOR
    if kind == "threat":
        return geo.THREAT_MAP_COLOR
    return geo.EVENT_COLOR


def build_map(posts: pd.DataFrame | None, alerts: pd.DataFrame | None,
              days: int | None = 30) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = None if not days else now - timedelta(days=int(days))
    events: list[dict] = []
    threats: list[dict] = []
    seen: set[tuple] = set()
    geo.reset_nominatim_budget(10)

    def add_event(*, origin, place, when, source, url, text, kind):
        hit = geo.geocode(place)
        if not hit or not geo.in_australia(hit["lat"], hit["lon"]):
            return
        key = (origin, place, url or text[:40], kind)
        if key in seen:
            return
        seen.add(key)
        events.append({
            "kind": kind,
            "origin": origin,
            "lat": hit["lat"], "lon": hit["lon"],
            "place": place, "label": place,
            "date": when.strftime("%Y-%m-%d") if when else "",
            "source": source, "url": url or "",
            "color": _color_for(kind),
            "text": _s(text)[:180],
            "style": "hollow" if kind == "planned" else "solid",
        })

    if alerts is not None and not alerts.empty:
        for r in alerts.to_dict(orient="records"):
            when = _aware(r.get("posted_at")) or _aware(r.get("received_at"))
            event_when = _parse_date(str(r.get("text") or "")) or when
            upcoming = bool(event_when and event_when.date() > now.date())
            if not upcoming and cutoff and when and when < cutoff:
                continue
            if upcoming and cutoff and event_when and event_when < now - timedelta(days=1):
                pass
            places = _places_from_alert(r)
            src = (r.get("source") or "").lower()
            source = SOURCE_LABELS.get(src) or origin_label({"url": r.get("url") or "", "source": src})
            kind = _event_kind("", upcoming, "", str(r.get("text") or ""))
            for place in places:
                add_event(origin="alert", place=place, when=event_when or when,
                          source=source, url=r.get("url") or "", text=r.get("text") or "",
                          kind=kind)

    if posts is not None and not posts.empty:
        df = posts.copy()
        for r in df.to_dict(orient="records"):
            when = _aware(r.get("post_date"))
            nested = r.get("data") if isinstance(r.get("data"), dict) else {}
            data = {**nested, **{k: v for k, v in r.items() if k != "data" and _s(v)}}
            cat = str(r.get("category") or data.get("category") or "")
            refined = str(r.get("refined_category") or data.get("refined_category") or "")
            is_protest = cat in PROTEST_CATS or refined in PROTEST_CATS
            is_threat = cat in THREAT_CATS or refined in THREAT_CATS
            event_date_raw = r.get("event_date") or data.get("event_date") or ""
            event_when = _aware(event_date_raw) if event_date_raw else _parse_date(str(event_date_raw))
            if event_when is None:
                event_when = _parse_date(str(data.get("narrative_summary") or ""))
            upcoming = bool(is_protest and event_when and event_when.date() > now.date())
            if not upcoming and cutoff and when and when < cutoff:
                continue
            places = _split_places(r.get("protest_location") or data.get("protest_location"))
            link = r.get("link") or data.get("URL") or data.get("url") or data.get("link") or ""
            summary = _s(data.get("narrative_summary")) or _s(data.get("Hit Sentence")) or _s(data.get("hit sentence"))
            source = origin_label({**data, "link": link, "source name": r.get("source_name"),
                                   "origin_source": data.get("origin_source") or r.get("source_name")})
            status = str(r.get("mobilization_status") or data.get("mobilization_status") or "")
            if is_protest:
                kind = _event_kind(status, upcoming, refined, summary)
                for place in places:
                    add_event(origin="post", place=place, when=event_when or when,
                              source=source, url=link, text=summary, kind=kind)
            if is_threat:
                targets = _split_places(r.get("targets") or data.get("targets"))
                for place in targets[:3]:
                    hit = geo.geocode(place)
                    if not hit or not geo.in_australia(hit["lat"], hit["lon"]):
                        continue
                    key = ("threat", r.get("document_id"), place)
                    if key in seen:
                        continue
                    seen.add(key)
                    threats.append({
                        "kind": "threat", "origin": "post",
                        "lat": hit["lat"], "lon": hit["lon"],
                        "place": place, "label": place,
                        "date": when.strftime("%Y-%m-%d") if when else "",
                        "source": source,
                        "url": link,
                        "color": geo.THREAT_MAP_COLOR,
                        "text": _s(summary)[:180],
                        "threat_type": str(r.get("threat_type") or data.get("threat_type") or ""),
                    })

    assets = []
    for marker in geo.asset_markers():
        assets.append({**marker, "short": _short_label(marker["label"])})

    return {
        "assets": assets,
        "events": events,
        "threats": threats,
        "days": days,
        "colors": {
            "asset": geo.ASSET_COLOR, "past": geo.EVENT_COLOR,
            "planned": geo.UPCOMING_COLOR, "advocacy": geo.ADVOCACY_COLOR,
            "threat": geo.THREAT_MAP_COLOR,
        },
        "bounds": {"south": geo.AU_SOUTH, "north": geo.AU_NORTH,
                   "west": geo.AU_WEST, "east": geo.AU_EAST},
        "legend": [
            {"label": "Sites", "color": geo.ASSET_COLOR, "swatch": "site"},
            {"label": "Threats", "color": geo.THREAT_MAP_COLOR, "swatch": "fill"},
            {"label": "Past protests", "color": geo.EVENT_COLOR, "swatch": "fill"},
            {"label": "Planned protests", "color": geo.UPCOMING_COLOR, "swatch": "hollow"},
            {"label": "Advocacy", "color": geo.ADVOCACY_COLOR, "swatch": "fill"},
        ],
    }
