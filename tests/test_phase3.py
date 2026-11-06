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
    return pd.DataFrame([
        {"Document ID": "1", "Date": "2026-09-03", "URL": "https://x.com/a/status/1", "link": "https://x.com/a/status/1",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 50, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "violence", "targets": "Base A", "heat_score": 8, "heat_topic": "military bases"},
        {"Document ID": "2", "Date": "2026-10-03", "URL": "https://x.com/a/status/2", "link": "https://x.com/a/status/2",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 10, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "violence", "targets": "Base A; Minister B", "heat_score": 6, "heat_topic": "military bases"},
        {"Document ID": "3", "Date": "2026-10-05", "URL": "https://x.com/c/status/3", "link": "https://x.com/c/status/3",
         "Author Handle": "@bad", "Author Name": "Bad", "Engagement": 5, "category": "threat", "is_flagged": True,
         "is_genuine": True, "threat_type": "doxxing", "targets": "Base A"},
        {"Document ID": "4", "Date": "2026-10-06", "URL": "https://x.com/d/status/4", "link": "https://x.com/d/status/4",
         "Author Handle": "@org", "Author Name": "Org", "Engagement": 7, "category": "protest", "is_flagged": True,
         "protest_location": "Perth", "protest_topic": "submarines", "organizers_or_groups": "Group C"},
    ])


def test_unified_merges_alerts_and_drops_url_duplicates(tmp_path):
    from triage import unified
    st = Storage(f"sqlite:///{tmp_path}/d.db")
    now = datetime.now(timezone.utc)
    st.add_alert(parse_alert(dict(ALERT, url="https://bsky.app/x/1", source="bluesky",
                                  text="We will bomb the AUKUS shipyard")), now=now)
    st.add_alert(parse_alert(dict(ALERT, url="https://x.com/a/status/99", text="nice weather")), now=now)
    posts = pd.DataFrame([{"Document ID": "1", "Date": now.strftime("%Y-%m-%d"), "post_date": now.replace(tzinfo=None),
                           "URL": "https://x.com/a/status/99", "link": "https://x.com/a/status/99",
                           "category": "threat", "is_flagged": True, "Hit Sentence": "kill the AUKUS deal"}])
    st.save_run(rows_to_store(posts, posts), ["s.xlsx"], 1)
    df = unified.load(st)
    assert len(df) == 2  # stored post + the bluesky alert; "nice weather" fails the pre-sort
    assert set(df["item_type"].fillna("post")) == {"post", "alert"}


def test_dashboard_view_has_threat_and_protest_sections():
    df = _posts()
    ctx = dash.build(df, {("post", "2"): "relevant"})
    t, p = ctx["threats"], ctx["protests"]
    assert t["count"] == 3 and p["count"] == 1
    assert t["authors"][0]["label"] == "@bad" and len(t["authors"][0]["posts"]) == 3
    assert t["targets"][0]["label"] == "Base A"
    assert {x["label"].lower() for x in t["topics"]} == {"violence", "doxxing"}
    assert p["topics"][0]["label"] == "submarines"
    assert t["download"] == "/download/threats.xlsx" and p["download"] == "/download/protests.xlsx"
    assert {c["handle"] for c in t["posts"]} == {"@bad"} and t["posts"][0]["platform"]


def test_dashboard_renders_with_data(client):
    from triage import unified
    st = Storage()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    df = _posts().assign(post_date=now, Date=now.strftime("%Y-%m-%d"))
    st.save_run(rows_to_store(df, df), ["s.xlsx"], 4)
    login(client)
    html = client.get("/dashboard", headers=auth()).get_data(as_text=True)
    for s in ("Threats", "Protests", "targets", "authors", "Flagged posts", "post-row",
              "/download/threats.xlsx", "/download/protests.xlsx"):
        assert s in html
    for gone in ("Early warning", "Monthly trend", "Upcoming protests", "Trish", "google-analytics"):
        assert gone not in html


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
