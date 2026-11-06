"""Geocoding for the dashboard map: Nominatim with Postgres cache and AU city fallbacks."""
from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, MetaData, String, Table, Text, create_engine, select

from .storage import database_url, normalize_database_url, persistence_enabled

log = logging.getLogger(__name__)
USER_AGENT = "ThreatProtestMonitor/1.0 (dashboard map; contact: ops@localhost)"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
_lock = threading.Lock()
_last_call = 0.0
_nominatim_budget = {"left": 8}
_engine = None
metadata = MetaData()

geocode_cache = Table(
    "geocode_cache", metadata,
    Column("query_key", String(255), primary_key=True),
    Column("display_name", Text),
    Column("lat", Float),
    Column("lon", Float),
    Column("source", String(32), nullable=False),  # nominatim | centroid | miss
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Fixed defense and civic assets (label, lat, lon, kind).
# kind selects the icon: anchor (naval/port), plane (air), building (HQ/shipyard/industry),
# shield (army/base). Civic sites use the building icon.
ASSETS = [
    ("Osborne Naval Shipyard", -34.788, 138.506, "shipyard"),
    ("HMAS Stirling", -32.24, 115.69, "naval"),
    ("Henderson", -32.163, 115.778, "shipyard"),
    ("Pine Gap", -23.799, 133.726, "base"),
    ("RAAF Tindal", -14.52, 132.38, "air"),
    ("Australian Submarine Agency HQ (Majura Park)", -35.2997, 149.1886, "hq"),
    ("BAE Systems Edinburgh", -34.7199, 138.6532, "industry"),
    ("Russell Offices", -35.2981, 149.1510, "hq"),
    ("US Embassy Canberra", -35.3064, 149.1167, "hq"),
    ("US Consulate Sydney", -33.8401, 151.2064, "hq"),
    ("US Consulate Melbourne", -37.8469, 144.9801, "hq"),
    ("Naval Communication Station Harold Holt", -21.8187, 114.1647, "naval"),
    ("HMAS Kuttabul", -33.8672, 151.2263, "naval"),
    ("NSW Parliament", -33.8673, 151.2127, "hq"),
    ("Sydney Town Hall", -33.8732, 151.2062, "hq"),
    ("Perth Convention and Exhibition Centre", -31.9566, 115.8524, "hq"),
]

# Icon family for each site kind. White glyph on a blue disc; always shown.
ICON_FOR_KIND = {
    "naval": "anchor", "port": "anchor",
    "air": "plane",
    "hq": "building", "shipyard": "building", "industry": "building", "civic": "building",
    "army": "shield", "base": "shield",
}

# Australia-only view. Markers outside this box are not drawn.
AU_SOUTH, AU_NORTH = -45.0, -9.0
AU_WEST, AU_EAST = 110.0, 155.5

# Built-in Australian city / region centroids used when Nominatim misses or is offline.
CITY_CENTROIDS = {
    "sydney": (-33.8688, 151.2093),
    "melbourne": (-37.8136, 144.9631),
    "brisbane": (-27.4698, 153.0251),
    "perth": (-31.9505, 115.8605),
    "adelaide": (-34.9285, 138.6007),
    "canberra": (-35.2809, 149.1300),
    "hobart": (-42.8821, 147.3272),
    "darwin": (-12.4634, 130.8456),
    "newcastle": (-32.9283, 151.7817),
    "wollongong": (-34.4278, 150.8931),
    "geelong": (-38.1499, 144.3617),
    "townsville": (-19.2590, 146.8169),
    "cairns": (-16.9186, 145.7781),
    "toowoomba": (-27.5598, 151.9507),
    "ballarat": (-37.5622, 143.8503),
    "bendigo": (-36.7570, 144.2794),
    "launceston": (-41.4332, 147.1441),
    "mackay": (-21.1411, 149.1860),
    "rockhampton": (-23.3781, 150.5080),
    "bunbury": (-33.3271, 115.6414),
    "albany": (-35.0228, 117.8814),
    "fremantle": (-32.0569, 115.7439),
    "rockingham": (-32.2760, 115.7298),
    "geraldton": (-28.7744, 114.6085),
    "kalgoorlie": (-30.7460, 121.4650),
    "alice springs": (-23.6980, 133.8807),
    "broome": (-17.9614, 122.2359),
    "karratha": (-20.7377, 116.8463),
    "port hedland": (-20.3107, 118.6070),
    "whyalla": (-33.0327, 137.5610),
    "mount gambier": (-37.8280, 140.7827),
    "mildura": (-34.1855, 142.1625),
    "wagga wagga": (-35.1082, 147.3598),
    "albury": (-36.0737, 146.9135),
    "orange": (-33.2830, 149.1000),
    "dubbo": (-32.2569, 148.6011),
    "tamworth": (-31.0927, 150.9320),
    "coffs harbour": (-30.2963, 153.1135),
    "port macquarie": (-31.4308, 152.9089),
    "nowra": (-34.8740, 150.6020),
    "gosford": (-33.4267, 151.3417),
    "sunshine coast": (-26.6500, 153.0667),
    "gold coast": (-28.0167, 153.4000),
    "northern territory": (-19.4914, 132.5510),
    "western australia": (-25.0423, 121.6450),
    "south australia": (-30.0002, 136.2092),
    "queensland": (-20.9176, 142.7028),
    "new south wales": (-31.2532, 146.9211),
    "victoria": (-36.9848, 143.5847),
    "tasmania": (-42.0409, 146.8087),
    "australian capital territory": (-35.4735, 149.0124),
    "australia": (-25.2744, 133.7751),
    "united states": (39.8283, -98.5795),
    "united kingdom": (55.3781, -3.4360),
}

# Site discs are blue with a white glyph. Event fills are red / orange / purple.
ASSET_COLOR = "#1f6feb"
EVENT_COLOR = "#ff8a1f"          # past protest, filled
EVENT_ALERT_COLOR = "#c792ea"    # advocacy
UPCOMING_COLOR = "#ff8a1f"       # planned protest, hollow stroke
THREAT_MAP_COLOR = "#e23b32"
ADVOCACY_COLOR = "#c792ea"


def australia_land() -> dict:
    """Natural Earth 110m Australia outline (public domain), for the low-zoom basemap."""
    import json
    from pathlib import Path
    path = Path(__file__).with_name("australia_110m.geojson")
    return json.loads(path.read_text(encoding="utf-8"))


def in_australia(lat: float, lon: float) -> bool:
    return AU_SOUTH <= lat <= AU_NORTH and AU_WEST <= lon <= AU_EAST


def icon_for(kind: str) -> str:
    return ICON_FOR_KIND.get(kind or "", "building")


def _eng():
    global _engine
    if _engine is None:
        _engine = create_engine(normalize_database_url(database_url()), pool_pre_ping=True)
        metadata.create_all(_engine)
    return _engine


def _key(place: str) -> str:
    return re.sub(r"\s+", " ", (place or "").strip().lower())[:255]


def _centroid(place: str) -> tuple[float, float] | None:
    key = _key(place)
    if key in CITY_CENTROIDS:
        return CITY_CENTROIDS[key]
    for name, coords in CITY_CENTROIDS.items():
        if name in key or key in name:
            return coords
    # "Perth, WA" / "Sydney NSW"
    for name, coords in CITY_CENTROIDS.items():
        if re.search(rf"\b{re.escape(name)}\b", key):
            return coords
    return None


def _cache_get(key: str) -> dict | None:
    if not persistence_enabled():
        return None
    try:
        with _eng().connect() as conn:
            row = conn.execute(select(geocode_cache).where(geocode_cache.c.query_key == key)).mappings().first()
        return dict(row) if row else None
    except Exception as exc:
        log.warning("Geocode cache read failed: %s", type(exc).__name__)
        return None


def _cache_put(key: str, display: str | None, lat, lon, source: str) -> None:
    if not persistence_enabled():
        return
    try:
        from sqlalchemy import delete
        now = datetime.now(timezone.utc)
        with _eng().begin() as conn:
            conn.execute(delete(geocode_cache).where(geocode_cache.c.query_key == key))
            conn.execute(geocode_cache.insert().values(
                query_key=key, display_name=display, lat=lat, lon=lon, source=source, updated_at=now))
    except Exception as exc:
        log.warning("Geocode cache write failed: %s", type(exc).__name__)


def _nominatim(place: str) -> tuple[float, float, str] | None:
    global _last_call
    with _lock:
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1, "countrycodes": "au,us,gb"})
        req = urllib.request.Request(f"{NOMINATIM}?{q}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                import json
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.warning("Nominatim failed: %s", type(exc).__name__)
            data = []
        finally:
            _last_call = time.monotonic()
    if not data:
        return None
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name") or place


def reset_nominatim_budget(n: int = 8) -> None:
    _nominatim_budget["left"] = n


def geocode(place: str) -> dict | None:
    """Return {lat, lon, label, source} or None."""
    place = (place or "").strip()
    if not place or len(place) < 2:
        return None
    key = _key(place)
    cached = _cache_get(key)
    if cached:
        if cached.get("source") == "miss" or cached.get("lat") is None:
            # Still try centroid on a prior miss.
            c = _centroid(place)
            if c:
                return {"lat": c[0], "lon": c[1], "label": place, "source": "centroid"}
            return None
        return {"lat": cached["lat"], "lon": cached["lon"],
                "label": cached.get("display_name") or place, "source": cached["source"]}
    c = _centroid(place)
    if c and ("," not in place and len(place.split()) <= 3):
        # Prefer known AU centroids for short place names to spare Nominatim.
        _cache_put(key, place, c[0], c[1], "centroid")
        return {"lat": c[0], "lon": c[1], "label": place, "source": "centroid"}
    if _nominatim_budget["left"] <= 0:
        if c:
            return {"lat": c[0], "lon": c[1], "label": place, "source": "centroid"}
        return None
    _nominatim_budget["left"] -= 1
    hit = _nominatim(place if "australia" in key or "," in place else f"{place}, Australia")
    if hit:
        lat, lon, display = hit
        _cache_put(key, display, lat, lon, "nominatim")
        return {"lat": lat, "lon": lon, "label": display, "source": "nominatim"}
    if c:
        _cache_put(key, place, c[0], c[1], "centroid")
        return {"lat": c[0], "lon": c[1], "label": place, "source": "centroid"}
    _cache_put(key, place, None, None, "miss")
    return None


_SHORT = {
    "Osborne Naval Shipyard": "Osborne",
    "Australian Submarine Agency HQ (Majura Park)": "ASA Majura",
    "BAE Systems Edinburgh": "BAE Edinburgh",
    "Naval Communication Station Harold Holt": "Harold Holt",
    "Perth Convention and Exhibition Centre": "Perth CEC",
    "US Embassy Canberra": "US Embassy",
    "US Consulate Sydney": "US Consulate SYD",
    "US Consulate Melbourne": "US Consulate MEL",
}
# Nudge labels apart where sites sit on top of each other at country zoom.
_SIDE = {
    "US Consulate Sydney": "left",
    "NSW Parliament": "left",
    "Sydney Town Hall": "right",
    "Russell Offices": "left",
    "US Embassy Canberra": "right",
    "Australian Submarine Agency HQ (Majura Park)": "right",
}


def asset_markers() -> list[dict]:
    return [{
        "kind": "asset", "label": name, "short": _SHORT.get(name, name),
        "lat": lat, "lon": lon, "site_kind": site_kind, "icon": icon_for(site_kind),
        "side": _SIDE.get(name, "right"), "color": ASSET_COLOR, "always": True,
    } for name, lat, lon, site_kind in ASSETS]
