import pandas as pd
import pytest

from triage import geo
from triage.mapdata import build_map


def test_assets_complete():
    assert len(geo.asset_markers()) == 16
    assert geo.ASSETS[0][0] == "Osborne Naval Shipyard"


def test_city_centroid_geocode(monkeypatch):
    monkeypatch.setattr(geo, "persistence_enabled", lambda: False)
    hit = geo.geocode("Perth")
    assert hit and abs(hit["lat"] + 31.9505) < 0.01


def test_map_colors_distinct():
    coloraide = pytest.importorskip("coloraide")
    cols = [geo.ASSET_COLOR, geo.EVENT_COLOR, geo.EVENT_ALERT_COLOR]
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            assert coloraide.Color(a).delta_e(b, method="2000") >= 20, (a, b)


def test_build_map_alert_location(monkeypatch):
    from datetime import datetime, timezone
    from triage import unified
    monkeypatch.setattr(geo, "persistence_enabled", lambda: False)
    alerts = pd.DataFrame([{
        "id": 1, "source": "seerist", "url": "https://x.com/a/status/1", "url_key": "x.com/a/status/1",
        "matched_terms": "News & Social; Location: Canberra; Sentiment: Negative",
        "text": "Join the rally outside Parliament House Saturday 10am to stop AUKUS",
        "posted_at": pd.Timestamp("2026-11-01", tz="UTC"),
        "received_at": pd.Timestamp("2026-11-01", tz="UTC"),
        "signal_level": "medium", "author": "x",
    }])
    raw = unified.combine(None, alerts, datetime(2026, 10, 1, tzinfo=timezone.utc))
    out = build_map(raw, days=None)
    assert out["events"] and out["events"][0]["place"] == "Canberra"
    assert out["events"][0]["source"] == "Seerist"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "pw")
    from app import create_app
    return create_app().test_client()


def test_dashboard_includes_map(client):
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'id="dash-map"' in html
    assert "Natural Earth" in html and "tile.openstreetmap.org" in html
    assert "leaflet" in html.lower() and "OpenStreetMap" in html
    assert 'data-layer="threats"' in html and 'data-layer="protests"' in html
    assert 'data-layer="past"' not in html and 'data-layer="planned"' not in html
    assert 'class="btn map-range"' not in html  # the map is always the last 30 days
    assert html.count('aria-pressed="true"') >= 2
    data = client.get("/api/map?days=30").get_json()
    assert len(data["assets"]) == 16 and "threats" in data
    icons = {a["icon"] for a in data["assets"]}
    assert icons == {"anchor", "plane", "building", "shield"}
    assert all(a.get("always") and a.get("short") for a in data["assets"])


def test_web_source_color_distinct():
    coloraide = pytest.importorskip("coloraide")
    from triage import dashboard as dash
    web = dash.SOURCE_COLORS["web"]
    from triage import geo
    others = [c for k, c in dash.SOURCE_COLORS.items() if k != "web"] + list(dash.COLORS.values()) + [geo.ASSET_COLOR]
    for o in others:
        assert coloraide.Color(web).delta_e(o, method="2000") >= 20, o


def test_basemap_and_phone_layout(client):
    html = client.get("/dashboard").get_data(as_text=True)
    assert "tile.openstreetmap.org" in html
    assert "getZoom()>=7" in html or "getZoom() >= 7" in html
    assert "basemaps.cartocdn.com" not in html
    assert "World_Dark_Gray_Base" not in html
    assert "API KEY REQUIRED" not in html
    assert "@media (max-width:390px)" in html
    assert "map-labels.js" in html and "zoomend moveend" in html
    assert "paddingTopLeft" in html and "maxZoom: narrow ? 4 : 5" in html
    assert ".dash-map{height:520px}" in html
    assert "map-label{font-size:13px}" in html
    assert "map-label{font-size:11px}" not in html
    assert "map-label{font-size:10px}" not in html
    assert "bindTooltip" not in html
    assert "post-row" in html
    assert "google-analytics" not in html and "Trish" not in html
    assert "Meltwater" not in html.split('id="map-legend"')[-1].split("</section>")[0]
