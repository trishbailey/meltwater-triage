"""One dataset for the dashboard: Meltwater posts, Seerist posts, and monitored-channel alerts.

Meltwater and Seerist uploads already share one table (a Seerist item whose URL matches a
Meltwater post is merged into it). Alerts arrive through the API and are stored apart, so
they are read here, screened with the same pre-sort rules, and appended in the same layout.
An alert whose URL is already stored as a post is dropped as a duplicate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .alerts import normalize_url

import os

WINDOW_DAYS = 30  # kept for callers that want a rolling window; the dashboard uses START_DATE
START_DATE = os.environ.get("DASHBOARD_START_DATE", "2026-11-01")  # posts before this are not shown
SOURCE_LABELS = {"x": "X", "bluesky": "Bluesky", "facebook": "Facebook", "instagram": "Instagram",
                 "newsletter": "Newsletter", "seerist": "Seerist", "web": "Web"}


def start() -> datetime:
    return datetime.fromisoformat(START_DATE).replace(tzinfo=timezone.utc)


def cutoff(days: int | None = None, now: datetime | None = None) -> datetime:
    """Earliest post time shown: the start date, or `days` back if that is later."""
    first = start()
    if days is None:
        return first
    return max(first, (now or datetime.now(timezone.utc)) - timedelta(days=days))


def _alert_rows(alerts: pd.DataFrame, since: datetime) -> pd.DataFrame:
    """Alerts in the column layout of a stored post, pre-sorted like an upload."""
    if alerts is None or alerts.empty:
        return pd.DataFrame()
    rows = []
    for r in alerts.to_dict(orient="records"):
        when = r.get("posted_at") if not pd.isna(r.get("posted_at")) else r.get("received_at")
        when = pd.Timestamp(when)
        when = when.tz_localize("UTC") if when.tzinfo is None else when.tz_convert("UTC")
        if when < since:
            continue
        places = [t.split(":", 1)[1].strip() for t in str(r.get("matched_terms") or "").split(";")
                  if t.strip().lower().startswith("location:")]
        src = str(r.get("source") or "").lower()
        rows.append({
            "Document ID": f"alert:{r['id']}", "item_type": "alert", "item_id": str(r["id"]),
            "Date": when.strftime("%Y-%m-%d %H:%M:%S"), "post_date": when.tz_convert("UTC").tz_localize(None),
            "Hit Sentence": r.get("text") or "", "URL": r.get("url") or "", "link": r.get("url") or "",
            "Author Name": r.get("author") or "", "Author Handle": "", "Engagement": 0,
            "Location": "; ".join(places), "origin_source": SOURCE_LABELS.get(src, src.title()),
            "export_source": SOURCE_LABELS.get(src, src.title()) + " alert",
            "_url_key": r.get("url_key") or normalize_url(r.get("url") or "")})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    from .filters import screen_dataframe
    screened = screen_dataframe(df.drop(columns=["_url_key"]))
    screened["_url_key"] = df["_url_key"].to_numpy()
    return screened[screened["category"].fillna("") != ""].reset_index(drop=True)


def combine(posts: pd.DataFrame | None, alerts: pd.DataFrame | None, since: datetime) -> pd.DataFrame:
    """Stored flagged posts plus screened alerts, newest window only."""
    base = posts if posts is not None else pd.DataFrame()
    extra = _alert_rows(alerts, since)
    if not extra.empty and not base.empty:
        link_cols = [c for c in ("link", "URL") if c in base.columns]
        seen = set()
        for c in link_cols:
            seen |= {normalize_url(str(v)) for v in base[c].dropna() if str(v).strip()}
        extra = extra[~extra["_url_key"].isin(seen)]
    extra = extra.drop(columns=["_url_key"], errors="ignore")
    frames = [f for f in (base, extra) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    return out[out["category"].fillna("").astype(str) != ""].reset_index(drop=True)


def load(storage, days: int | None = None, now: datetime | None = None) -> pd.DataFrame:
    """The last `days` days of flagged posts and alerts, or an empty frame without storage."""
    if storage is None:
        return pd.DataFrame()
    since = cutoff(days, now)
    return combine(storage.load_posts(flagged_only=True, since=since), storage.load_alerts(limit=500), since)
