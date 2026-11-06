"""Scheduled collection of Facebook, Instagram and Bluesky posts into the alerts table.

Facebook and Instagram run through Apify actors (APIFY_TOKEN). Bluesky uses its own search API
(BLUESKY_HANDLE and BLUESKY_APP_PASSWORD). Each post becomes an alert; the dashboard applies the
same pre-sort to alerts as to uploaded posts. Duplicates are dropped by normalized URL.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .alerts import AlertError, parse_alert

log = logging.getLogger(__name__)

# The look-back window is the gap between runs plus two hours of overlap. The Render cron job runs
# twice a day (every 12 hours), so the default is 16 hours (the longest gap between the 06:00 and 20:00 UTC runs is 14); duplicates are dropped by URL.
WINDOW_HOURS = int(os.environ.get("COLLECT_WINDOW_HOURS", "16"))
WINDOW = f"{WINDOW_HOURS} hours"
RESULTS_PER_ACCOUNT = 10

INSTAGRAM_ACCOUNTS = [
    "stopaukuswa", "ipanaustralia", "right2protestwa", "wollongongagainstwarandnukes",
    "anti.aukuscoalition", "noaukuscoalitionvic", "wagepeaceau", "nuclear_free_wa",
    "wa.anti.war.collective", "tomorrowmvmt",
]
FACEBOOK_PAGES = [
    "https://www.facebook.com/stopaukuswa",
    "https://www.facebook.com/IndependentAndPeacefulAustraliaNetwork",
    "https://www.facebook.com/noaukusvic",
    "https://www.facebook.com/AustralianAntiAUKUSCoalition",
    "https://www.facebook.com/right2protestwa",
    "https://www.facebook.com/WollongongAgainstWarandNukes",
    "https://www.facebook.com/uraniumfree.wa",
    "https://www.facebook.com/SydAntiAukusCoalition",
    "https://www.facebook.com/RejectAUKUSRockingham/",
]
BLUESKY_QUERIES = ["AUKUS", "Pine Gap", "HMAS Stirling", "Osborne shipyard", "AUKUS protest", "AUKUS rally"]

ACTOR_INSTAGRAM = "apify~instagram-post-scraper"
ACTOR_FACEBOOK = "apify~facebook-posts-scraper"


def _http(url: str, data: dict | None = None, headers: dict | None = None, timeout: int = 300):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "null")


def run_actor(actor: str, payload: dict, token: str, fetch=_http) -> list[dict]:
    """Run an Apify actor and return its dataset items (waits up to ~5 minutes)."""
    url = (f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?"
           + urllib.parse.urlencode({"token": token, "timeout": 280, "clean": "true"}))
    out = fetch(url, payload, None, 300)
    return out if isinstance(out, list) else []


def _alert(source: str, url: str, author: str, text: str, when: str | None) -> dict | None:
    if not url or not (text or "").strip():
        return None
    try:
        return parse_alert({"source": source, "url": url, "author": author or None, "text": text,
                            "posted_at": when or None, "signal_level": "low"})
    except AlertError as exc:
        log.warning("skipped %s item: %s", source, exc)
        return None


def instagram_alerts(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if it.get("error"):
            continue
        a = _alert("instagram", it.get("url") or "", it.get("ownerUsername") or "",
                   it.get("caption") or "", it.get("timestamp"))
        if a:
            out.append(a)
    return out


def facebook_alerts(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if it.get("error"):
            continue
        page = it.get("pageName")
        author = (page.get("name") if isinstance(page, dict) else page) \
            or (it.get("user") or {}).get("name") or ""
        a = _alert("facebook", it.get("url") or it.get("topLevelUrl") or "", author,
                   it.get("text") or "", it.get("time"))
        if a:
            out.append(a)
    return out


def bluesky_alerts(handle: str, password: str, queries=BLUESKY_QUERIES, fetch=_http,
                   now: datetime | None = None) -> list[dict]:
    session = fetch("https://bsky.social/xrpc/com.atproto.server.createSession",
                    {"identifier": handle, "password": password}, None, 30)
    auth = {"Authorization": f"Bearer {session['accessJwt']}"}
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, seen = [], set()
    for q in queries:
        params = urllib.parse.urlencode({"q": q, "since": since, "limit": 100, "sort": "latest"})
        data = fetch(f"https://bsky.social/xrpc/app.bsky.feed.searchPosts?{params}", None, auth, 30) or {}
        for p in data.get("posts", []):
            uri = p.get("uri", "")
            if uri in seen:
                continue
            seen.add(uri)
            h = (p.get("author") or {}).get("handle", "")
            rkey = uri.rsplit("/", 1)[-1]
            rec = p.get("record") or {}
            a = _alert("bluesky", f"https://bsky.app/profile/{h}/post/{rkey}", h, rec.get("text", ""),
                       rec.get("createdAt"))
            if a:
                out.append(a)
    return out


def collect(storage, fetch=_http, env=None, now: datetime | None = None) -> dict:
    """Pull every configured source and store new posts as alerts. Returns a per-source summary."""
    env = os.environ if env is None else env
    summary: dict = {}
    jobs = []
    token = env.get("APIFY_TOKEN")
    if token:
        jobs.append(("instagram", lambda: instagram_alerts(run_actor(
            ACTOR_INSTAGRAM, {"username": INSTAGRAM_ACCOUNTS, "resultsLimit": RESULTS_PER_ACCOUNT,
                              "onlyPostsNewerThan": WINDOW, "dataDetailLevel": "basicData",
                              "skipPinnedPosts": False, "includeMusicData": False}, token, fetch))))
        jobs.append(("facebook", lambda: facebook_alerts(run_actor(
            ACTOR_FACEBOOK, {"startUrls": [{"url": u} for u in FACEBOOK_PAGES],
                             "resultsLimit": RESULTS_PER_ACCOUNT, "onlyPostsNewerThan": WINDOW,
                             "captionText": False}, token, fetch))))
    else:
        summary["instagram"] = summary["facebook"] = "skipped: APIFY_TOKEN not set"
    if env.get("BLUESKY_HANDLE") and env.get("BLUESKY_APP_PASSWORD"):
        jobs.append(("bluesky", lambda: bluesky_alerts(env["BLUESKY_HANDLE"], env["BLUESKY_APP_PASSWORD"],
                                                       fetch=fetch, now=now)))
    else:
        summary["bluesky"] = "skipped: BLUESKY_HANDLE or BLUESKY_APP_PASSWORD not set"
    summary["x"] = "skipped: not configured"
    for name, run in jobs:
        try:
            alerts = run()
            new = sum(1 for a in alerts if storage.add_alert(a, now=now)[1])
            summary[name] = {"fetched": len(alerts), "new": new}
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            log.exception("%s collection failed", name)
            summary[name] = f"failed: {type(exc).__name__}: {str(exc)[:200]}"
    return summary


def review_backlog(storage) -> int:
    """Have Claude review flagged posts that have no stored review yet (respects the monthly cap)."""
    from .pipeline import backfill_flagged
    return backfill_flagged(storage)


def _slot(now: datetime, hours: list[int]) -> str:
    """Identifier of the most recent scheduled slot at or before `now` (UTC)."""
    day = now.date()
    past = [h for h in hours if h <= now.hour]
    if not past:
        day, past = day - timedelta(days=1), hours
    return f"{day}T{max(past):02d}"


def start_scheduler(storage, logger=log, env=None, sleep=None):
    """Run collect() at fixed UTC hours (COLLECT_HOURS, default 10 and 22) in a background thread.

    Does nothing unless a source is configured. The first slot after a start is skipped, so a
    deploy does not trigger an extra pull. Duplicates are dropped by URL if two instances overlap.
    """
    import threading
    import time
    env = os.environ if env is None else env
    if env.get("COLLECT_SCHEDULE", "1") == "0":
        return None
    if not (env.get("APIFY_TOKEN") or (env.get("BLUESKY_HANDLE") and env.get("BLUESKY_APP_PASSWORD"))):
        return None
    hours = sorted({int(h) for h in env.get("COLLECT_HOURS", "6,20").split(",") if h.strip()})
    pause = sleep or time.sleep

    def loop():
        last = _slot(datetime.now(timezone.utc), hours)
        while True:
            pause(300)
            slot = _slot(datetime.now(timezone.utc), hours)
            if slot == last:
                continue
            last = slot
            try:
                logger.info("collection %s: %s", slot, collect(storage))
            except Exception:  # noqa: BLE001
                logger.exception("collection %s failed", slot)
            try:
                logger.info("review %s: %d posts reviewed", slot, review_backlog(storage))
            except Exception:  # noqa: BLE001
                logger.exception("review %s failed", slot)

    t = threading.Thread(target=loop, name="collector", daemon=True)
    t.start()
    return t
