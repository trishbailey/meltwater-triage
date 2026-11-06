import base64
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from triage import dashboard as dash
from triage.alerts import AlertError, normalize_url, parse_alert
from triage.storage import Storage, rows_to_store

PW = "pw-test"
TOKEN = "tok-test"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", PW)
    monkeypatch.setenv("ALERT_INGEST_TOKEN", TOKEN)
    from app import create_app, _failures
    _failures.clear()
    return create_app().test_client()


def login(c, pw=PW):
    return c.post("/login", data={"password": pw})


def auth(pw=PW):
    return {}


def bearer(t=TOKEN):
    return {"Authorization": f"Bearer {t}"}


ALERT = {"source": "x", "url": "https://twitter.com/a/status/1?s=20", "signal_level": "high",
         "author": "a", "text": "rally at the base", "matched_terms": ["rally"]}


def test_normalize_url():
    assert normalize_url("https://www.Twitter.com/a/status/1/?s=20#x") == "x.com/a/status/1"
    assert normalize_url("http://x.com/a/status/1") == "x.com/a/status/1"
    assert normalize_url("https://www.facebook.com/permalink.php?story_fbid=1") != \
        normalize_url("https://www.facebook.com/permalink.php?story_fbid=2")


@pytest.mark.parametrize("bad", [None, [], {"source": "tiktok", "url": "https://a.b", "signal_level": "low"},
                                 {"source": "x", "url": "ftp://a.b", "signal_level": "low"},
                                 {"source": "x", "url": "https://a.b", "signal_level": "huge"},
                                 {"source": "x", "url": "https://a.b", "signal_level": "low", "posted_at": "nope"}])
def test_parse_alert_rejects(bad):
    with pytest.raises(AlertError):
        parse_alert(bad)


def test_alert_auth_and_dedupe(client):
    assert client.post("/api/alerts", json=ALERT).status_code == 401
    assert client.post("/api/alerts", json=ALERT, headers=bearer("wrong")).status_code == 401
    assert client.post("/api/alerts", json={"source": "x"}, headers=bearer()).status_code == 400
    r1 = client.post("/api/alerts", json=ALERT, headers=bearer())
    assert r1.status_code == 201
    dup = dict(ALERT, url="https://x.com/a/status/1", source="x")
    r2 = client.post("/api/alerts", json=dup, headers=bearer())
    assert r2.status_code == 200 and r2.get_json()["duplicate"] and r2.get_json()["id"] == r1.get_json()["id"]


def test_alert_ingest_unconfigured(client, monkeypatch):
    monkeypatch.delenv("ALERT_INGEST_TOKEN")
    assert client.post("/api/alerts", json=ALERT, headers=bearer()).status_code == 503












def test_delete_alert(client):
    rid = client.post("/api/alerts", json=ALERT, headers=bearer()).get_json()["id"]
    assert client.post(f"/alerts/{rid}/delete", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(f"/alerts/{rid}/delete").status_code == 302
    assert client.post(f"/alerts/{rid}/delete").status_code == 404
    assert Storage().load_alerts().empty


def _posts():
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=40)).strftime("%Y-%m-%d")
    d2 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    d3 = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    d4 = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return pd.DataFrame([
        {"Document ID": "1", "Date": old, "URL": "https://x.com/a/status/1", "link": "https://x.com/a/status/1",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 50, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "violence", "targets": "Base A", "heat_score": 8, "heat_topic": "military bases",
         "Hit Sentence": "I will bomb the AUKUS base at HMAS Stirling."},
        {"Document ID": "2", "Date": d2, "URL": "https://x.com/a/status/2", "link": "https://x.com/a/status/2",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 10, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "violence", "targets": "Base A; Minister B", "heat_score": 6, "heat_topic": "military bases",
         "Hit Sentence": "We will attack HMAS Stirling next week."},
        {"Document ID": "3", "Date": d3, "URL": "https://x.com/c/status/3", "link": "https://x.com/c/status/3",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 5, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "doxxing", "targets": "Base A",
         "Hit Sentence": "I will publish the home address of the defence minister."},
        {"Document ID": "4", "Date": d4, "URL": "https://x.com/d/status/4", "link": "https://x.com/d/status/4",
         "Author Handle": "@org", "Author Name": "Org", "Engagement": 7, "category": "protest", "is_flagged": True,
         "protest_location": "Perth", "protest_topic": "submarines", "organizers_or_groups": "Group C",
         "Hit Sentence": "Join us for a rally in Perth this Saturday."},
    ])


def test_alerts_layer_is_separate_and_confirmed(tmp_path):
    st = Storage(f"sqlite:///{tmp_path}/d.db")
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    st.add_alert(parse_alert(dict(ALERT, url="https://www.twitter.com/a/status/2/")), now=t0)
    st.add_alert(parse_alert(dict(ALERT, url="https://bsky.app/x/1", source="bluesky")), now=t0)
    df = _posts()
    st.save_run(rows_to_store(df, df), ["s.xlsx"], 4, now=t0 + timedelta(hours=5))
    ctx = dash.build(st.load_posts(), st.load_alerts(), st.meltwater_first_seen(), month="2026-10")
    a = ctx["alerts"]
    assert a["total"] == 2 and a["confirmed"] == 1
    confirmed = [i for i in a["items"] if i["confirmed"]][0]
    assert confirmed["lead"] == "5.0 hours"
    # Meltwater totals unaffected by alerts
    assert ctx["headline"][0]["value"] == 2 and ctx["headline"][1]["value"] == 1
    assert "delta" not in ctx["headline"][0]
    assert ctx["volume"] == []
    assert ctx["posters"][0]["handle"] == "@bad" and ctx["posters"][0]["count"] == 2
    assert ctx["targets"][0]["label"] == "Base A"
    assert ctx["protest_locations"][0]["label"] == "Perth"
    assert ctx["heat"][0]["label"] == "Military bases"


def test_dashboard_renders_with_data(client):
    st = Storage()
    df = _posts()
    st.save_run(rows_to_store(df, df), ["s.xlsx"], 4)
    client.post("/api/alerts", json=ALERT, headers=bearer())
    login(client)
    html = client.get("/dashboard?month=2026-10", headers=auth()).get_data(as_text=True)
    for s in ("Recurring threat posters", "Early warning", "Threat ranking", "last 30 days",
              "Upcoming protests", "post-row", "not added to the threat or protest totals"):
        assert s in html
    assert "Monthly trend" not in html and "Last 90 days" not in html and "Advocacy" not in html
    assert "Trish" not in html and "google-analytics" not in html
    status = client.get("/status", headers=auth()).get_data(as_text=True)
    assert "rows" in status.lower() or "post" in status.lower()


def test_palette_distinct():
    coloraide = pytest.importorskip("coloraide")
    cols = list(dash.COLORS.values()) + list(dash.SOURCE_COLORS.values())
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            assert coloraide.Color(a).delta_e(b, method="2000") >= 20, (a, b)


def test_seerist_color_distinct_from_everything():
    coloraide = pytest.importorskip("coloraide")
    seerist = dash.SOURCE_COLORS["seerist"]
    others = [c for k, c in dash.SOURCE_COLORS.items() if k != "seerist"]
    others += list(dash.COLORS.values()) + list(dash.SIGNAL_COLORS.values())
    for o in others:
        assert coloraide.Color(seerist).delta_e(o, method="2000") >= 20, o


def test_pages_public_and_robots(client):
    home = client.get("/")
    assert home.status_code == 302 and "/dashboard" in home.headers["Location"]
    for path in ("/dashboard", "/status", "/upload"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'name="robots" content="noindex, nofollow"' in r.get_data(as_text=True)
    assert client.get("/robots.txt").get_data(as_text=True) == "User-agent: *\nDisallow: /\n"
    assert client.post("/api/alerts", json=ALERT).status_code == 401  # token still required
