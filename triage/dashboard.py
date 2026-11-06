"""Builds the /dashboard view model: a 30-day map, then Threats, then Protests.

Input is the unified frame from triage.unified (Meltwater posts, Seerist posts and alerts in
one layout). Posts the AI review rejected are left out of the views and kept in the downloads.
Posts the review has not reached yet stay in as pre-sorted posts.
"""
from __future__ import annotations

import pandas as pd

from .filters import DROPPED_PROTEST_STATUSES
from .grouping import group_indices
from .export import (PROTEST_CATS, THREAT_CATS, author_handle, is_rejected, post_text)
from .origin import origin_label
from .taxonomy import canon_targets, canon_topic, targets_from_text
from . import unified

COLORS = {"threat": "#c92f24", "protest": "#c8a24a"}
SOURCE_COLORS = {"x": "#5aa9ff", "bluesky": "#2fd6b5", "facebook": "#b48cff",
                 "instagram": "#ff6fae", "newsletter": "#8fd14f",
                 "seerist": "#ffee00",
                 "web": "#fd6b00"}
SOURCE_LABELS = {"x": "X", "bluesky": "Bluesky", "facebook": "Facebook",
                 "instagram": "Instagram", "newsletter": "Newsletter",
                 "seerist": "Seerist",
                 "web": "Web"}
# One-hue red ramp (lighter = less severe), most severe first.
SIGNAL_COLORS = {"critical": "#ff4d3d", "high": "#e8705f", "medium": "#d99a8f", "low": "#f0c9c2"}
TOP_N = 8
MAX_GROUPS = 120
POST_CHARS = 700
PENDING = "Analysis pending"


def _bars(items: list[dict], key: str = "value") -> list[dict]:
    top = max((it[key] for it in items), default=0) or 1
    for it in items:
        it["pct"] = round(100 * it[key] / top, 1)
    return items


def _s(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def _num(v) -> float:
    try:
        n = float(v)
        return 0.0 if n != n else n
    except (TypeError, ValueError):
        return 0.0


def _when(rec: dict):
    for key in ("post_date", "Date", "date"):
        v = rec.get(key)
        if _s(v):
            ts = pd.to_datetime(v, errors="coerce")
            if not pd.isna(ts):
                return ts
    return None


def _view(df: pd.DataFrame, labels: dict, reasons: dict | None = None) -> list[dict]:
    """One flat record per post: only what the page shows."""
    out = []
    for rec in df.to_dict(orient="records"):
        if is_rejected(rec.get("is_genuine")):
            continue
        if _s(rec.get("category")) == "protest" and _s(rec.get("mobilization_status")) in DROPPED_PROTEST_STATUSES:
            continue  # advocacy campaigns and past-event recaps are not flagged any more
        kind = _s(rec.get("item_type")) or "post"
        item_id = _s(rec.get("item_id")) or _s(rec.get("document_id")) or _s(rec.get("Document ID"))
        when = _when(rec)
        reviewed = bool(_s(rec.get("refined_category")) or _s(rec.get("narrative_summary")))
        out.append({
            "item_type": kind, "item_id": item_id,
            "platform": _s(rec.get("origin_source")) or origin_label(rec),
            "handle": author_handle(rec),
            "name": _s(rec.get("Author Name") or rec.get("author name")),
            "when": when, "date": when.strftime("%Y-%m-%d") if when is not None else "",
            "text": post_text(rec)[:POST_CHARS],
            "link": _s(rec.get("link")) or _s(rec.get("URL")) or _s(rec.get("url")),
            "eng": _num(rec.get("Engagement") if "Engagement" in rec else rec.get("engagement")),
            "category": _s(rec.get("category")),
            "threat_type": _s(rec.get("threat_type")), "theme": _s(rec.get("theme")),
            "targets": _s(rec.get("targets")), "protest_topic": _s(rec.get("protest_topic")),
            "summary": _s(rec.get("narrative_summary")), "reviewed": reviewed,
            "label": labels.get((kind, item_id), ""),
            "reason": (reasons or {}).get((kind, item_id), {}).get("reason", ""),
            "note": (reasons or {}).get((kind, item_id), {}).get("note", ""),
        })
    return out


def _target_counts(rows: list[dict], n: int = TOP_N) -> list[dict]:
    """Posts per grouped target. A post naming two variants of one target counts once."""
    counts: dict[str, list] = {}
    for r in rows:
        for label in _targets(r):
            rec = counts.setdefault(label.lower(), [label, 0])
            rec[1] += 1
    items = sorted(counts.values(), key=lambda x: (-x[1], x[0]))[:n]
    return _bars([{"label": a, "value": b} for a, b in items])


def _topics(rows: list[dict], kind: str) -> list[dict]:
    """Topic analysis: threat type for threats, grouped protest topic for protests."""
    groups: dict[str, dict] = {}
    for r in rows:
        if kind == "threat":
            label = r["threat_type"].strip().capitalize() or PENDING
        else:
            label = _topic_label(r, kind)
        g = groups.setdefault(label.lower(), {"label": label, "value": 0, "themes": {}})
        g["value"] += 1
        if kind == "threat" and r["theme"]:
            t = r["theme"].lower()
            g["themes"][t] = g["themes"].get(t, 0) + 1
    items = sorted(groups.values(), key=lambda g: (g["label"] == PENDING, g["label"] == "Other", -g["value"]))[:TOP_N]
    for g in items:
        g["themes"] = [t for t, _ in sorted(g["themes"].items(), key=lambda kv: -kv[1])[:3]]
    return _bars(items)


def _topic_label(r: dict, kind: str) -> str:
    if kind == "threat":
        return r["threat_type"].strip().capitalize() or PENDING
    # Until Claude's analysis exists, the keyword rules assign the topic from the post text.
    return canon_topic(r["protest_topic"]) or canon_topic(r["text"]) or PENDING


def _targets(r: dict) -> list[str]:
    return canon_targets(r["targets"]) if r["targets"] else targets_from_text(r["text"])


def _post_card(r: dict, kind: str) -> dict:
    card = {k: r[k] for k in ("item_type", "item_id", "platform", "handle", "date", "text", "link",
                              "label", "summary", "reviewed", "reason", "note")}
    # Used by the page to rebuild the analysis as the analyst marks posts.
    card["topic"] = _topic_label(r, kind)
    card["target_list"] = _targets(r)
    return card


def _sorted_posts(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["when"] is not None, r["when"] or pd.Timestamp(0), r["eng"]),
                  reverse=True)


def _groups(rows: list[dict], kind: str) -> list[dict]:
    """Near-duplicate posts in one group: the newest post represents the group."""
    ordered = _sorted_posts(rows)
    idx = group_indices([r["text"] for r in ordered], [r["link"] for r in ordered])
    out = []
    for g in idx:
        members = [_post_card(ordered[i], kind) for i in g]
        labels_ = {m["label"] for m in members}
        out.append({"rep": members[0], "members": members, "n": len(members),
                    "label": labels_.pop() if len(labels_) == 1 else "",
                    "reason": ({m["reason"] for m in members}.pop() if len({m["reason"] for m in members}) == 1 else ""),
                    "platforms": sorted({m["platform"] for m in members if m["platform"]})})
    return out


def _section(rows: list[dict], kind: str) -> dict:
    kept = [r for r in rows if r["label"] == "relevant"]  # analysis uses analyst-confirmed posts only
    reviewed = [r for r in rows if r["reviewed"]]
    pending = [r for r in rows if not r["reviewed"]]
    g_rev, g_pen = _groups(reviewed, kind), _groups(pending, kind)
    shown_rev, shown_pen = g_rev[:MAX_GROUPS], g_pen[:MAX_GROUPS]
    return {
        "kind": kind, "count": len(rows),
        "reviewed": len(reviewed), "pending": len(pending),
        "topics": _topics(kept, kind),
        "targets": _target_counts(kept),
        "groups": shown_rev, "pending_groups": shown_pen,
        "groups_total": len(g_rev), "pending_groups_total": len(g_pen),
        "more": sum(g["n"] for g in g_rev[MAX_GROUPS:]) + sum(g["n"] for g in g_pen[MAX_GROUPS:]),
        "relevant": len(kept),
        "download": f"/download/{kind}s.xlsx",
        "color": COLORS[kind],
    }


def build(raw: pd.DataFrame | None, labels: dict | None = None, storage_ready: bool = True,
          reasons: dict | None = None) -> dict:
    from . import geo
    labels = labels or {}
    ctx: dict = {"colors": COLORS, "window_label": "since " + unified.start().strftime("%B %-d"), "storage_ready": storage_ready,
                 "relevance_labels": {f"{t}:{i}": lab for (t, i), lab in labels.items()},
                 "land": geo.australia_land(),
                 "map": {"assets": geo.asset_markers(), "days": None,
                         "legend": [
                             {"label": "Sites", "color": geo.ASSET_COLOR, "swatch": "site"},
                             {"label": "Threats", "color": geo.THREAT_MAP_COLOR, "swatch": "fill"},
                             {"label": "Protests", "color": geo.EVENT_COLOR, "swatch": "fill"},
                         ]}}
    empty = raw is None or raw.empty or "category" not in raw.columns
    rows = [] if empty else _view(raw, labels, reasons)
    threat = [r for r in rows if r["category"] in THREAT_CATS]
    protest = [r for r in rows if r["category"] in PROTEST_CATS]
    ctx["threats"] = _section(threat, "threat")
    ctx["protests"] = _section(protest, "protest")
    ctx["has_data"] = bool(rows)
    return ctx
