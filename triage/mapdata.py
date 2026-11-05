"""Assemble Leaflet-ready markers for the dashboard map."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import geo
from .dashboard import SOURCE_LABELS, PROTEST_CATS, THREAT_CATS

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


def build_map(posts: pd.DataFrame | None, alerts: pd.DataFrame | None,
              days: int | None = 30) -> dict:
    now = datetime.now(timezone.utc)
    cutoff = None if not days else now - timedelta(days=int(days))
    events: list[dict] = []
    threats: list[dict] = []
    seen: set[tuple] = set()
    geo.reset_nominatim_budget(10)

    def add_event(*, origin, place, when, source, url, text, upcoming=False):
        hit = geo.geocode(place)
        if not hit:
            return
        key = (origin, place, url or text[:40], "up" if upcoming else "past")
        if key in seen:
            return
        seen.add(key)
        events.append({
            "kind": "upcoming" if upcoming else "event",
            "origin": origin,
            "lat": hit["lat"], "lon": hit["lon"],
            "place": place, "label": place,
            "date": when.strftime("%Y-%m-%d") if when else "",
            "source": source, "url": url or "",
            "color": geo.UPCOMING_COLOR if upcoming else (
                geo.EVENT_ALERT_COLOR if origin == "alert" else geo.EVENT_COLOR),
            "text": (text or "")[:180],
            "style": "dashed" if upcoming else "solid",
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
            # Prefer newsletter/web/seerist for planned protests
            for place in places:
                add_event(origin="alert", place=place, when=event_when or when,
                          source=SOURCE_LABELS.get(src, src or "Alert"),
                          url=r.get("url") or "", text=r.get("text") or "",
                          upcoming=upcoming)

    if posts is not None and not posts.empty:
        df = posts.copy()
        for r in df.to_dict(orient="records"):
            when = _aware(r.get("post_date"))
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
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
            link = r.get("link") or data.get("URL") or data.get("link") or ""
            summary = data.get("narrative_summary") or data.get("Hit Sentence") or ""
            if is_protest:
                for place in places:
                    add_event(origin="meltwater", place=place, when=event_when or when,
                              source="Meltwater", url=link, text=summary, upcoming=upcoming)
            if is_threat:
                targets = _split_places(r.get("targets") or data.get("targets"))
                for place in targets[:3]:
                    hit = geo.geocode(place)
                    if not hit:
                        continue
                    key = ("threat", r.get("document_id"), place)
                    if key in seen:
                        continue
                    seen.add(key)
                    threats.append({
                        "kind": "threat", "origin": "meltwater",
                        "lat": hit["lat"], "lon": hit["lon"],
                        "place": place, "label": place,
                        "date": when.strftime("%Y-%m-%d") if when else "",
                        "source": "Meltwater",
                        "url": link,
                        "color": geo.THREAT_MAP_COLOR,
                        "text": summary[:180],
                        "threat_type": str(r.get("threat_type") or data.get("threat_type") or ""),
                    })

    assets = []
    for name, lat, lon in geo.ASSETS:
        assets.append({
            "kind": "asset", "label": name, "short": _short_label(name),
            "lat": lat, "lon": lon, "color": geo.ASSET_COLOR,
        })

    return {
        "assets": assets,
        "events": events,
        "threats": threats,
        "days": days,
        "colors": {
            "asset": geo.ASSET_COLOR, "event": geo.EVENT_COLOR,
            "alert": geo.EVENT_ALERT_COLOR, "upcoming": geo.UPCOMING_COLOR,
            "threat": geo.THREAT_MAP_COLOR,
        },
        "legend": [
            {"label": "Defense and civic sites", "color": geo.ASSET_COLOR},
            {"label": "Past protest", "color": geo.EVENT_COLOR},
            {"label": "Upcoming protest", "color": geo.UPCOMING_COLOR},
            {"label": "Early-warning alert", "color": geo.EVENT_ALERT_COLOR},
            {"label": "Threat target", "color": geo.THREAT_MAP_COLOR},
        ],
    }
