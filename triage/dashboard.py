"""Builds the /dashboard view model: a 30-day map, then Threats, then Protests.

Input is the unified frame from triage.unified (Meltwater posts, Seerist posts and alerts in
one layout). Posts the AI review rejected are left out of the views and kept in the downloads.
Posts the review has not reached yet stay in as pre-sorted posts.
"""
from __future__ import annotations

import pandas as pd

from .export import (PROTEST_CATS, THREAT_CATS, author_handle, is_rejected, post_text)
from .origin import origin_label
from .unified import WINDOW_DAYS

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
MAX_POSTS = 150
POST_CHARS = 700
PENDING = "Not yet reviewed"


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


def _view(df: pd.DataFrame, labels: dict) -> list[dict]:
    """One flat record per post: only what the page shows."""
    out = []
    for rec in df.to_dict(orient="records"):
        if is_rejected(rec.get("is_genuine")):
            continue
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
        })
    return out


def _split_counts(rows: list[dict], field: str, n: int = TOP_N) -> list[dict]:
    counts: dict[str, list] = {}
    for r in rows:
        for part in r[field].split(";"):
            part = part.strip()
            if part:
                rec = counts.setdefault(part.lower(), [part, 0, 0.0])
                rec[1] += 1
                rec[2] += r["eng"]
    items = sorted(counts.values(), key=lambda x: (x[1], x[2]), reverse=True)[:n]
    return _bars([{"label": a, "value": b, "engagement": int(c)} for a, b, c in items])


def _topics(rows: list[dict], kind: str) -> list[dict]:
    """Topic analysis: threat type for threats, protest topic for protests."""
    field = "threat_type" if kind == "threat" else "protest_topic"
    groups: dict[str, dict] = {}
    for r in rows:
        label = r[field].strip() or PENDING
        g = groups.setdefault(label.lower(), {"label": label.capitalize() if kind == "threat" else label,
                                              "value": 0, "engagement": 0.0, "themes": {}})
        g["value"] += 1
        g["engagement"] += r["eng"]
        theme = r["theme"]
        if theme and kind == "threat":
            g["themes"][theme.lower()] = g["themes"].get(theme.lower(), 0) + 1
    items = sorted(groups.values(), key=lambda g: (g["label"] == PENDING, -g["value"], -g["engagement"]))[:TOP_N]
    for g in items:
        g["engagement"] = int(g["engagement"])
        g["themes"] = [t for t, _ in sorted(g["themes"].items(), key=lambda kv: -kv[1])[:3]]
    return _bars(items)


def _post_card(r: dict) -> dict:
    return {k: r[k] for k in ("item_type", "item_id", "platform", "handle", "date", "text", "link",
                              "label", "summary", "reviewed")}


def _sorted_posts(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["when"] is not None, r["when"] or pd.Timestamp(0), r["eng"]),
                  reverse=True)


def _authors(rows: list[dict], n: int = 10) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        key = (r["handle"] or r["name"]).strip().lower()
        if not key:
            continue
        g = groups.setdefault(key, {"handle": r["handle"] or r["name"], "value": 0, "engagement": 0.0,
                                    "platforms": set(), "rows": []})
        g["value"] += 1
        g["engagement"] += r["eng"]
        if r["platform"]:
            g["platforms"].add(r["platform"])
        g["rows"].append(r)
    items = sorted(groups.values(), key=lambda g: (g["value"], g["engagement"]), reverse=True)[:n]
    out = [{"label": g["handle"], "value": g["value"], "engagement": int(g["engagement"]),
            "platforms": ", ".join(sorted(g["platforms"])),
            "posts": [_post_card(r) for r in _sorted_posts(g["rows"])[:8]]} for g in items]
    return _bars(out)


def _section(rows: list[dict], kind: str) -> dict:
    ordered = _sorted_posts(rows)
    return {
        "kind": kind, "count": len(rows),
        "reviewed": sum(1 for r in rows if r["reviewed"]),
        "pending": sum(1 for r in rows if not r["reviewed"]),
        "topics": _topics(rows, kind),
        "targets": _split_counts(rows, "targets"),
        "authors": _authors(rows),
        "posts": [_post_card(r) for r in ordered[:MAX_POSTS]],
        "more": max(0, len(ordered) - MAX_POSTS),
        "download": f"/download/{kind}s.xlsx",
        "color": COLORS[kind],
    }


def build(raw: pd.DataFrame | None, labels: dict | None = None, storage_ready: bool = True) -> dict:
    from . import geo
    labels = labels or {}
    ctx: dict = {"colors": COLORS, "window_days": WINDOW_DAYS, "storage_ready": storage_ready,
                 "relevance_labels": {f"{t}:{i}": lab for (t, i), lab in labels.items()},
                 "land": geo.australia_land(),
                 "map": {"assets": geo.asset_markers(), "days": WINDOW_DAYS,
                         "legend": [
                             {"label": "Sites", "color": geo.ASSET_COLOR, "swatch": "site"},
                             {"label": "Threats", "color": geo.THREAT_MAP_COLOR, "swatch": "fill"},
                             {"label": "Protests", "color": geo.EVENT_COLOR, "swatch": "fill"},
                         ]}}
    empty = raw is None or raw.empty or "category" not in raw.columns
    rows = [] if empty else _view(raw, labels)
    threat = [r for r in rows if r["category"] in THREAT_CATS]
    protest = [r for r in rows if r["category"] in PROTEST_CATS]
    ctx["threats"] = _section(threat, "threat")
    ctx["protests"] = _section(protest, "protest")
    ctx["has_data"] = bool(rows)
    return ctx
