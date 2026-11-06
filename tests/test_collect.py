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
