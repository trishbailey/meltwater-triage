import urllib.parse
from datetime import datetime, timezone

from triage import collect as c
from triage.storage import Storage


def test_collect_maps_dedupes_and_reports(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'c.db'}")
    calls = []

    def fetch(url, data=None, headers=None, timeout=0):
        calls.append(url)
        if "instagram-post-scraper" in url:
            return [{"url": "https://www.instagram.com/p/a1/", "ownerUsername": "stopaukuswa",
                     "caption": "Rally at Stirling Saturday", "timestamp": "2026-11-06T01:00:00.000Z"},
                    {"url": "https://www.instagram.com/noaukuscoalitionvic", "error": "no_items"},
                    {"url": "https://www.instagram.com/p/a2/", "ownerUsername": "x", "caption": ""}]
        if "facebook-posts-scraper" in url:
            return [{"url": "https://www.facebook.com/noaukusvic/posts/9", "pageName": "No AUKUS Vic",
                     "text": "Join the blockade", "time": "2026-11-06T02:00:00.000Z"},
                    {"inputUrl": "https://www.facebook.com/uraniumfree.wa", "error": "no_items"}]
        if "createSession" in url:
            return {"accessJwt": "t"}
        return {"posts": [{"uri": "at://did:plc:x/app.bsky.feed.post/3k", "author": {"handle": "a.bsky.social"},
                           "record": {"text": "protest AUKUS", "createdAt": "2026-11-06T03:00:00Z"}}]}

    env = {"APIFY_TOKEN": "tok", "BLUESKY_HANDLE": "me", "BLUESKY_APP_PASSWORD": "pw"}
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    out = c.collect(st, fetch=fetch, env=env, now=now)
    assert out["instagram"] == {"fetched": 1, "new": 1}
    assert out["facebook"] == {"fetched": 1, "new": 1}
    assert out["bluesky"]["fetched"] == 1 and out["x"].startswith("skipped")
    again = c.collect(st, fetch=fetch, env=env, now=now)
    assert again["instagram"]["new"] == 0 and again["facebook"]["new"] == 0
    sources = set(st.load_alerts()["source"])
    assert sources == {"instagram", "facebook", "bluesky"}


def test_collect_skips_unconfigured_sources(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'d.db'}")
    out = c.collect(st, fetch=lambda *a, **k: [], env={})
    assert all(str(v).startswith("skipped") for v in out.values())


def test_slot_changes_at_scheduled_hours():
    from datetime import datetime, timezone
    hours = [10, 22]
    t = lambda d, h: datetime(2026, 11, d, h, 5, tzinfo=timezone.utc)
    assert c._slot(t(6, 9), hours) == "2026-11-05T22"
    assert c._slot(t(6, 10), hours) == "2026-11-06T10"
    assert c._slot(t(6, 21), hours) == "2026-11-06T10"
    assert c._slot(t(6, 22), hours) == "2026-11-06T22"


def test_scheduler_needs_a_configured_source(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'s.db'}")
    assert c.start_scheduler(st, env={}) is None
    assert c.start_scheduler(st, env={"APIFY_TOKEN": "t", "COLLECT_SCHEDULE": "0"}) is None


def test_x_pull_takes_top_posts_only(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'x.db'}")
    seen = []

    def fetch(url, data=None, headers=None, timeout=0):
        seen.append((url, headers))
        return {"data": [{"id": str(i), "author_id": "u1", "text": f"AUKUS post {i}",
                          "created_at": "2026-11-06T03:00:00.000Z"} for i in range(15)],
                "includes": {"users": [{"id": "u1", "username": "someone"}]}}

    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    out = c.collect(st, fetch=fetch, env={"X_BEARER_TOKEN": "tok"}, now=now)
    assert out["x"] == {"fetched": 10, "new": 10}  # both searches return the same ids; duplicates dropped
    assert len(seen) == 2
    url, headers = seen[0]
    assert "sort_order=relevancy" in url and "max_results=10" in url and headers == {"Authorization": "Bearer tok"}
    assert "rally" in urllib.parse.unquote_plus(url) and "recency" in seen[1][0]
    assert set(st.load_alerts()["source"]) == {"x"}


def test_x_calls_are_capped_per_day(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'cap.db'}")
    calls = []
    fetch = lambda url, data=None, headers=None, timeout=0: calls.append(url) or {}
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    env = {"X_BEARER_TOKEN": "tok"}
    for _ in range(4):
        c.collect(st, fetch=fetch, env=env, now=now)
    assert len(calls) == 4  # two runs of two searches; later runs are skipped
    assert c.collect(st, fetch=fetch, env=env, now=now)["x"].startswith("skipped: daily X call limit")


def test_collect_once_runs_one_time_per_value(tmp_path, monkeypatch):
    st = Storage(f"sqlite:///{tmp_path/'o.db'}")
    ran = []
    monkeypatch.setattr(c, "collect", lambda s: ran.append(1) or {})
    monkeypatch.setattr(c, "review_backlog", lambda s: 0)
    t = c.run_once_on_start(st, env={"COLLECT_ONCE": "2026-11-06"})
    t.join(5)
    assert ran == [1]
    assert c.run_once_on_start(st, env={"COLLECT_ONCE": "2026-11-06"}) is None
    assert c.run_once_on_start(st, env={}) is None
    c.run_once_on_start(st, env={"COLLECT_ONCE": "next"}).join(5)
    assert ran == [1, 1]
