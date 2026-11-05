"""Assemble Leaflet-ready markers for the dashboard map."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from . import geo
from .alerts import SOURCES
from .dashboard import SOURCE_LABELS, PROTEST_CATS


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
    terms = row.get("matched_terms") or ""
    for t in str(terms).split(";"):
        t = t.strip()
        if t.lower().startswith("location:"):
            places.append(t.split(":", 1)[1].strip())
        elif t and not t.lower().startswith("sentiment:") and "news & social" not in t.lower():
            # Seerist type tags etc. are not places; skip bare types
            if t.lower() in {"news & social", "events", "news"}:
                continue
    # Countries from Seerist often sit in matched_terms as "Location: X" already handled.
    text = row.get("text") or ""
    return places


def build_map(posts: pd.DataFrame | None, alerts: pd.DataFrame | None,
              days: int | None = 30) -> dict:
    """Return {assets, events, days, colors, legend} for the map panel."""
    now = datetime.now(timezone.utc)
    cutoff = None if not days else now - timedelta(days=int(days))
    geo.reset_nominatim_budget(8)
    events: list[dict] = []
    seen: set[tuple] = set()

    if alerts is not None and not alerts.empty:
        for r in alerts.to_dict(orient="records"):
            when = _aware(r.get("posted_at")) or _aware(r.get("received_at"))
            if cutoff and when and when < cutoff:
                continue
            places = _places_from_alert(r)
            # Also try authorless country tags already parsed into matched_terms Location:
            if not places:
                continue
            for place in places:
                hit = geo.geocode(place)
                if not hit:
                    continue
                key = ("alert", r.get("id"), round(hit["lat"], 4), round(hit["lon"], 4))
                if key in seen:
                    continue
                seen.add(key)
                src = (r.get("source") or "").lower()
                events.append({
                    "kind": "event", "origin": "alert",
                    "lat": hit["lat"], "lon": hit["lon"],
                    "place": place, "label": place,
                    "date": when.strftime("%Y-%m-%d") if when else "",
                    "source": SOURCE_LABELS.get(src, src or "Alert"),
                    "url": r.get("url") or "",
                    "color": geo.EVENT_ALERT_COLOR,
                    "text": (r.get("text") or "")[:180],
                })

    if posts is not None and not posts.empty:
        df = posts.copy()
        mask = pd.Series(False, index=df.index)
        if "category" in df.columns:
            mask = mask | df["category"].isin(PROTEST_CATS)
        if "refined_category" in df.columns:
            mask = mask | df["refined_category"].isin(PROTEST_CATS)
        df = df[mask]
        for r in df.to_dict(orient="records"):
            when = _aware(r.get("post_date"))
            if cutoff and when and when < cutoff:
                continue
            places = _split_places(r.get("protest_location"))
            # Fall back to fields nested in data JSON
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            if not places:
                places = _split_places(data.get("protest_location"))
            link = r.get("link") or data.get("URL") or data.get("link") or ""
            for place in places:
                hit = geo.geocode(place)
                if not hit:
                    continue
                key = ("mw", r.get("document_id"), place)
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "kind": "event", "origin": "meltwater",
                    "lat": hit["lat"], "lon": hit["lon"],
                    "place": place, "label": place,
                    "date": when.strftime("%Y-%m-%d") if when else "",
                    "source": "Meltwater",
                    "url": link,
                    "color": geo.EVENT_COLOR,
                    "text": (data.get("narrative_summary") or data.get("Hit Sentence") or "")[:180],
                })

    return {
        "assets": geo.asset_markers(),
        "events": events,
        "days": days,
        "colors": {"asset": geo.ASSET_COLOR, "event": geo.EVENT_COLOR, "alert": geo.EVENT_ALERT_COLOR},
        "legend": [
            {"label": "Defense and civic sites", "color": geo.ASSET_COLOR},
            {"label": "Protest (Meltwater)", "color": geo.EVENT_COLOR},
            {"label": "Early-warning alert", "color": geo.EVENT_ALERT_COLOR},
        ],
    }
