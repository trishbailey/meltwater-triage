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
    monkeypatch.setattr(geo, "persistence_enabled", lambda: False)
    alerts = pd.DataFrame([{
        "id": 1, "source": "seerist", "url": "https://x.com/a/status/1",
        "matched_terms": "News & Social; Location: Canberra; Sentiment: Negative",
        "text": "rally", "posted_at": pd.Timestamp("2026-11-01", tz="UTC"),
        "received_at": pd.Timestamp("2026-11-01", tz="UTC"),
        "signal_level": "medium", "author": "x",
    }])
    out = build_map(pd.DataFrame(), alerts, days=90)
    assert out["events"] and out["events"][0]["place"] == "Canberra"


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
    assert 'id="dash-map"' in html and "World_Dark_Gray_Base" in html
    assert "leaflet" in html.lower() and "OpenStreetMap" in html
    data = client.get("/api/map?days=30").get_json()
    assert len(data["assets"]) == 16


def test_web_source_color_distinct():
    coloraide = pytest.importorskip("coloraide")
    from triage import dashboard as dash
    web = dash.SOURCE_COLORS["web"]
    others = [c for k, c in dash.SOURCE_COLORS.items() if k != "web"] + list(dash.COLORS.values())
    for o in others:
        assert coloraide.Color(web).delta_e(o, method="2000") >= 20, o


def test_esri_tiles_in_dashboard(client):
    html = client.get("/dashboard").get_data(as_text=True)
    assert "World_Dark_Gray_Base" in html
    assert "basemaps.cartocdn.com" not in html
    assert "Esri" in html
