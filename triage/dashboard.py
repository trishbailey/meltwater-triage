"""Builds the /dashboard view model from stored Meltwater posts and early-warning alerts.

Alerts are a separate, source-tagged layer and never enter Meltwater totals.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .aggregate import recurring_threat_posters
from .alerts import SIGNAL_LEVELS, SOURCES, normalize_url

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
THREAT_CATS = {"threat", "both"}
PROTEST_CATS = {"protest", "both"}
TOP_N = 5


def _col(df, name):
    for c in df.columns:
        if str(c).strip().lower() == name:
            return c
    return None


def _num(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)


def _month_label(m: str) -> str:
    try:
        return datetime.strptime(m, "%Y-%m").strftime("%B %Y")
    except (TypeError, ValueError):
        return str(m)


def _split_counts(series: pd.Series, weights: pd.Series | None = None, n: int = TOP_N) -> list[dict]:
    counts: dict[str, list] = {}
    for i, v in series.items():
        if not isinstance(v, str) or not v.strip():
            continue
        for part in v.split(";"):
            part = part.strip()
            if not part:
                continue
            key = part.lower()
            rec = counts.setdefault(key, [part, 0, 0.0])
            rec[1] += 1
            rec[2] += float(weights.get(i, 0)) if weights is not None else 0
    items = sorted(counts.values(), key=lambda r: (r[1], r[2]), reverse=True)[:n]
    return _bars([{"label": a, "value": b, "engagement": int(c)} for a, b, c in items])


def _bars(items: list[dict], key: str = "value") -> list[dict]:
    top = max((it[key] for it in items), default=0) or 1
    for it in items:
        it["pct"] = round(100 * it[key] / top, 1)
    return items


def _text(row, cols) -> str:
    return " ".join(str(row[c]) for c in cols if c and isinstance(row.get(c), str))[:400]


def build(posts: pd.DataFrame, alerts: pd.DataFrame | None = None,
          meltwater_seen: dict | None = None, month: str | None = None,
          map_days: int | None = 30) -> dict:
    ctx: dict = {"colors": COLORS, "source_colors": SOURCE_COLORS, "source_labels": SOURCE_LABELS,
                 "signal_colors": SIGNAL_COLORS, "has_data": not posts.empty}
    ctx["alerts"] = build_alerts(alerts, meltwater_seen or {})
    ctx.setdefault("threat_chart", {"rows": []})
    from . import geo
    ctx["map"] = {"assets": geo.asset_markers(), "days": map_days,
                 "colors": {"asset": geo.ASSET_COLOR, "event": geo.EVENT_COLOR,
                            "alert": geo.EVENT_ALERT_COLOR},
                 "legend": [{"label": "Defense and civic sites", "color": geo.ASSET_COLOR},
                            {"label": "Protest (Meltwater)", "color": geo.EVENT_COLOR},
                            {"label": "Early-warning alert", "color": geo.EVENT_ALERT_COLOR}]}
    if posts.empty:
        return ctx
    df = posts.copy()
    df["category"] = df.get("category", pd.Series("", index=df.index)).fillna("")
    df["eng"] = _num(df[_col(df, "engagement")]) if _col(df, "engagement") else 0
    df["is_threat"] = df["category"].isin(THREAT_CATS)
    df["is_protest"] = df["category"].isin(PROTEST_CATS)
    genuine = df["is_genuine"] if "is_genuine" in df else pd.Series(None, index=df.index)
    df["confirmed"] = genuine.map(lambda v: v is True or v == 1 or str(v).lower() == "true")
    months = sorted(m for m in df["post_month"].dropna().unique())
    if not months:
        ctx["has_data"] = False
        return ctx
    cur = month if month in months else months[-1]
    prev = months[months.index(cur) - 1] if months.index(cur) > 0 else None
    ctx.update(months=[{"key": m, "label": _month_label(m)} for m in months], month=cur,
               month_label=_month_label(cur), prev_label=_month_label(prev) if prev else None)

    def stats(m):
        d = df[df["post_month"] == m]
        heat = pd.to_numeric(d.get("heat_score"), errors="coerce") if "heat_score" in d else pd.Series(dtype=float)
        return {"threats": int(d["is_threat"].sum()),
                "confirmed_threats": int((d["is_threat"] & d["confirmed"]).sum()),
                "protests": int(d["is_protest"].sum()),
                "confirmed_protests": int((d["is_protest"] & d["confirmed"]).sum()),
                "avg_heat": round(float(heat.mean()), 1) if heat.notna().any() else None,
                "heat_posts": int(heat.notna().sum())}

    s_cur, s_prev = stats(cur), stats(prev) if prev else None

    def delta(k):
        if not s_prev or s_cur[k] is None or s_prev[k] is None:
            return None
        return round(s_cur[k] - s_prev[k], 1)

    ctx["headline"] = [
        {"label": "Threat posts", "value": s_cur["threats"], "delta": delta("threats"), "color": COLORS["threat"]},
        {"label": "Protest posts", "value": s_cur["protests"], "delta": delta("protests"), "color": COLORS["protest"]},
        {"label": "Average heat (0 to 10)", "value": s_cur["avg_heat"] if s_cur["avg_heat"] is not None else "n/a",
         "delta": delta("avg_heat"), "color": "#e6e9ef"},
    ]

    vol = []
    for m in months:
        d = df[df["post_month"] == m]
        vol.append({"label": _month_label(m), "threat": int(d["is_threat"].sum()),
                    "protest": int(d["is_protest"].sum())})
    top = max([v["threat"] for v in vol] + [v["protest"] for v in vol] + [1])
    for v in vol:
        v["threat_pct"], v["protest_pct"] = round(100 * v["threat"] / top, 1), round(100 * v["protest"] / top, 1)
    ctx["volume"] = vol

    m = df[df["post_month"] == cur]
    t = m[m["is_threat"]]
    p = m[m["is_protest"]]
    if "threat_type" in t and t["threat_type"].fillna("").ne("").any():
        g = (t[t["threat_type"].fillna("").ne("") & t["threat_type"].ne("none")]
             .groupby("threat_type").agg(value=("eng", "sum"), posts=("eng", "size")).reset_index()
             .sort_values("value", ascending=False).head(TOP_N))
        ctx["threat_types"] = _bars([{"label": r.threat_type.capitalize(), "value": int(r.value),
                                      "posts": int(r.posts)} for r in g.itertuples()])
    else:
        ctx["threat_types"] = []
    ctx["targets"] = _split_counts(t.get("targets", pd.Series(dtype=object)), t["eng"])
    ctx["threat_chart"] = build_threat_chart(t)
    ctx["people"] = _split_counts(m[m["is_threat"] | m["is_protest"]].get("named_individuals", pd.Series(dtype=object)), m["eng"])

    posters = recurring_threat_posters(t, min_count=2) if not t.empty else pd.DataFrame()
    text_cols = [_col(df, c) for c in ("hit sentence", "opening text", "title")]
    hc, nc = _col(t, "author handle"), _col(t, "author name")
    plist = []
    for r in posters.head(10).itertuples() if not posters.empty else []:
        key = (r.author_handle or r.author_name or "").lower()
        mine = t[(t[hc].fillna("").str.lower() == key) | (t[nc].fillna("").str.lower() == key)] if hc and nc else t.iloc[0:0]
        plist.append({"handle": r.author_handle, "name": r.author_name, "count": int(r.threat_posts),
                      "engagement": int(r.total_engagement),
                      "first": str(r.first_seen)[:10], "last": str(r.last_seen)[:10],
                      "posts": [{"text": _text(row, text_cols), "link": row.get("link"),
                                 "summary": row.get("narrative_summary"),
                                 "type": row.get("threat_type")} for _, row in mine.head(10).iterrows()]})
    ctx["posters"] = plist

    ctx["protest_locations"] = _split_counts(p.get("protest_location", pd.Series(dtype=object)), p["eng"])
    ctx["protest_topics"] = _split_counts(p.get("protest_topic", pd.Series(dtype=object)), p["eng"])
    ctx["organizers"] = _split_counts(p.get("organizers_or_groups", pd.Series(dtype=object)), p["eng"])

    heat = m[pd.to_numeric(m.get("heat_score"), errors="coerce").notna()] if "heat_score" in m else m.iloc[0:0]
    if not heat.empty and "heat_topic" in heat:
        h = heat.assign(hs=pd.to_numeric(heat["heat_score"], errors="coerce"))
        h = h[h["heat_topic"].fillna("").ne("")]
        g = (h.groupby("heat_topic").agg(avg=("hs", "mean"), total=("hs", "sum"), posts=("hs", "size"),
                                          eng=("eng", "sum")).reset_index()
             .sort_values(["avg", "total"], ascending=False).head(TOP_N))
        ctx["heat"] = [{"label": r.heat_topic.capitalize(), "avg": round(r.avg, 1), "total": int(r.total),
                        "posts": int(r.posts), "engagement": int(r.eng), "pct": round(10 * r.avg, 1)}
                       for r in g.itertuples()]
    else:
        ctx["heat"] = []
    ctx["summary"] = _summaries(ctx, s_cur, s_prev)
    return ctx


def _n(k, word):
    return f"{k} {word}" if k == 1 else f"{k} {word}s"


def _change(cur, prev, noun):
    if prev is None:
        return f"{cur} {noun}"
    if cur == prev:
        return f"{cur} {noun}, unchanged from the previous month"
    word = "up" if cur > prev else "down"
    return f"{cur} {noun}, {word} from {prev} the previous month"


def _summaries(ctx, s, sp) -> list[dict]:
    out = []
    out.append({"title": "Threats", "text": (
        f"Keyword screening flagged {_change(s['threats'], sp['threats'] if sp else None, 'posts with threat language')}. "
        f"Review confirmed {s['confirmed_threats']} of them as genuine threats."
        + (f" The leading threat type by engagement is {ctx['threat_types'][0]['label'].lower()}." if ctx.get("threat_types") else "")
        + (f" {_n(len(ctx['posters']), 'account')} posted threat language more than once." if ctx.get("posters") else
           " No account posted threat language more than once."))})
    out.append({"title": "Protests", "text": (
        f"Screening flagged {_change(s['protests'], sp['protests'] if sp else None, 'posts with protest or mobilization language')}. "
        f"Review confirmed {s['confirmed_protests']} as genuine protest activity."
        + (f" The most cited location is {ctx['protest_locations'][0]['label']}." if ctx.get("protest_locations") else "")
        + (f" The most active organizer is {ctx['organizers'][0]['label']}." if ctx.get("organizers") else ""))})
    if ctx.get("heat"):
        h = ctx["heat"][0]
        out.append({"title": "Heated language", "text": (
            f"Across {_n(s['heat_posts'], 'scored post')}, the average heat is {s['avg_heat']} on a 0 to 10 scale. "
            f"The most heated topic is {h['label'].lower()}, with an average of {h['avg']} across {_n(h['posts'], 'post')}.")})
    a = ctx.get("alerts") or {}
    if a.get("items"):
        out.append({"title": "Early warning", "text": (
            f"{_n(a['total'], 'early-warning alert')} arrived from monitored channels; {a['confirmed']} later appeared in Meltwater"
            + (f", a median {a['median_lead']} ahead." if a.get("median_lead") else "."))})
    return out


def _fmt_lead(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return _n(max(1, round(seconds / 60)), "minute")
    if hours < 48:
        return f"{round(hours, 1)} hours"
    return f"{round(hours / 24, 1)} days"


def _aware(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    ts = pd.Timestamp(v)
    if pd.isna(ts):
        return None
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts



def build_threat_chart(df: pd.DataFrame) -> dict:
    """Threat posts by target with type, for the dashboard graphic under the map."""
    if df is None or df.empty:
        return {"rows": []}
    rows = []
    eng = _num(df[_col(df, "engagement")]) if _col(df, "engagement") else pd.Series(0, index=df.index)
    for i, r in df.iterrows():
        cat = str(r.get("category") or "")
        refined = str(r.get("refined_category") or "")
        if cat not in THREAT_CATS and refined not in THREAT_CATS:
            continue
        targets = [t.strip() for t in str(r.get("targets") or "").split(";") if t.strip()]
        ttype = str(r.get("threat_type") or "unspecified").strip() or "unspecified"
        if not targets:
            targets = ["Unspecified target"]
        for t in targets[:2]:
            rows.append({"label": t, "type": ttype, "engagement": float(eng.get(i, 0)), "value": 1})
    if not rows:
        return {"rows": []}
    # aggregate
    bag: dict[tuple, dict] = {}
    for r in rows:
        key = (r["label"].lower(), r["type"].lower())
        rec = bag.setdefault(key, {"label": r["label"], "type": r["type"], "value": 0, "engagement": 0.0})
        rec["value"] += 1
        rec["engagement"] += r["engagement"]
    items = sorted(bag.values(), key=lambda x: (x["value"], x["engagement"]), reverse=True)[:12]
    top = max(it["value"] for it in items) or 1
    for it in items:
        it["pct"] = round(100 * it["value"] / top, 1)
        it["engagement"] = int(it["engagement"])
    return {"rows": items}


def build_alerts(alerts: pd.DataFrame | None, meltwater_seen: dict) -> dict:
    if alerts is None or alerts.empty:
        return {"items": [], "total": 0, "confirmed": 0, "by_source": [], "median_lead": None}
    seen = {k: _aware(v) for k, v in meltwater_seen.items()}
    items, leads = [], []
    for r in alerts.to_dict(orient="records"):
        first = _aware(r.get("received_at"))
        mw = seen.get(r.get("url_key") or normalize_url(r.get("url") or ""))
        lead = None
        if mw is not None and first is not None:
            secs = (mw - first).total_seconds()
            if secs > 0:
                leads.append(secs)
                lead = _fmt_lead(secs)
        items.append({**r, "confirmed": mw is not None, "lead": lead,
                      "first_seen": first.strftime("%Y-%m-%d %H:%M UTC") if first is not None else "",
                      "terms": [t for t in (r.get("matched_terms") or "").split("; ") if t]})
    order = {lvl: i for i, lvl in enumerate(SIGNAL_LEVELS)}
    items.sort(key=lambda it: (order.get(it["signal_level"], 9), it["first_seen"]), reverse=False)
    by_source = []
    for src in SOURCES:
        n = sum(1 for it in items if it["source"] == src)
        if n:
            by_source.append({"source": src, "label": SOURCE_LABELS[src], "value": n, "color": SOURCE_COLORS[src]})
    by_source = _bars(sorted(by_source, key=lambda b: b["value"], reverse=True))
    median = _fmt_lead(sorted(leads)[len(leads) // 2]) if leads else None
    return {"items": items[:50], "total": len(items), "confirmed": sum(it["confirmed"] for it in items),
            "by_source": by_source, "median_lead": median}
