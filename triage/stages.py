"""Claude stages that run after the pre-sort.

1. filter  (backfill_flagged for stored posts, review_alerts for collected alerts): Claude keeps or
   discards each candidate. No analysis.
2. analyze (analyze_relevant): topic, targets and the rest, only for posts an analyst marked Relevant.
"""
from __future__ import annotations

import logging
import threading

import pandas as pd

from . import keywords as kw
from . import unified

log = logging.getLogger(__name__)


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip() == ""


def _text_cols(df: pd.DataFrame) -> list:
    from .pipeline import _find_columns
    return _find_columns(df, kw.TEXT_COLUMN_OPTIONS)


def review_alerts(storage, complete=None) -> int:
    """Filter collected alerts that Claude has not judged yet. Returns how many were judged."""
    from .refine import is_enabled, refine
    if complete is None and not is_enabled():
        return 0
    df = unified.load(storage)
    if df.empty or "item_type" not in df.columns:
        return 0
    alerts = df[df["item_type"] == "alert"]
    if "is_genuine" in alerts.columns:
        alerts = alerts[alerts["is_genuine"].map(_blank)]
    if alerts.empty:
        return 0
    reviewed = refine(alerts.copy(), _text_cols(alerts), complete=complete, mode="filter")
    out = {}
    for rec in reviewed.to_dict(orient="records"):
        if rec.get("is_genuine") is None:
            continue
        out[int(rec["item_id"])] = {k: rec.get(k) for k in ("is_genuine", "refined_category")}
    return storage.merge_alert_reviews(out) if out else 0


def analyze_relevant(storage, complete=None) -> int:
    """Run the analysis on posts and alerts marked Relevant that have no analysis yet."""
    from . import feedback as fb
    from .refine import is_enabled, refine
    if complete is None and not is_enabled():
        return 0
    labels = fb.all_labels()
    df = unified.load(storage)
    if df.empty:
        return 0
    kinds = df["item_type"].fillna("post") if "item_type" in df.columns else pd.Series("post", index=df.index)
    ids = (df["item_id"] if "item_id" in df.columns else pd.Series(None, index=df.index, dtype=object))
    for col in ("document_id", "Document ID"):
        if col in df.columns:
            ids = ids.where(ids.notna(), df[col])
    mine = [labels.get((k, str(i))) == "relevant" for k, i in zip(kinds, ids)]
    need = df[mine]
    summary = need["narrative_summary"] if "narrative_summary" in need.columns else pd.Series("", index=need.index)
    need = need[summary.map(_blank)]
    if need.empty:
        return 0
    done = refine(need.copy(), _text_cols(need), complete=complete, mode="analyze")
    posts_recs, alert_recs = [], {}
    for rec in done.to_dict(orient="records"):
        if _blank(rec.get("narrative_summary")) and _blank(rec.get("protest_topic")) and _blank(rec.get("threat_type")):
            continue
        rec.pop("is_genuine", None)  # a human decision is final; analysis never rejects a post
        if rec.get("item_type") == "alert":
            alert_recs[int(rec["item_id"])] = {k: rec.get(k) for k in (
                "narrative_summary", "theme", "severity", "threat_type", "targets", "named_individuals",
                "protest_location", "protest_topic", "organizers_or_groups", "event_date", "scale_or_turnout")}
        else:
            posts_recs.append(rec)
    n = storage.merge_reviews(posts_recs) if posts_recs else 0
    return n + (storage.merge_alert_reviews(alert_recs) if alert_recs else 0)


_RUN_LOCK = threading.Lock()


def run_all(storage, complete=None, notify: bool = True) -> dict:
    """Filter new candidates, then alert the analyst. Analysis runs only on the analyst's request."""
    from .pipeline import backfill_flagged
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "another run is in progress"}
    try:
        return _run_all(storage, complete, notify, backfill_flagged)
    finally:
        _RUN_LOCK.release()


def _run_all(storage, complete, notify, backfill_flagged) -> dict:
    out = {}
    for name, fn in (("filtered_posts", lambda: backfill_flagged(storage, complete=complete)),
                     ("filtered_alerts", lambda: review_alerts(storage, complete=complete))):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", name, type(exc).__name__)
            out[name] = f"failed: {type(exc).__name__}"
    if notify:
        try:
            from .notify import announce
            out["alert"] = announce(storage)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert step failed: %s", type(exc).__name__)
            out["alert"] = f"failed: {type(exc).__name__}"
    return out
