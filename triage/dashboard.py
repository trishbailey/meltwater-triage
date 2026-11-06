"""Builds the /dashboard view model from stored Meltwater posts and early-warning alerts.

Alerts are a separate, source-tagged layer and never enter Meltwater totals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
WINDOW_DAYS = 30
_HIDE_TYPE = {"", "none", "unknown", "unspecified", "nan", "null", "call to protest"}


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


def _window_start(now: datetime) -> datetime:
    return now - timedelta(days=WINDOW_DAYS)


def build(posts: pd.DataFrame, alerts: pd.DataFrame | None = None,
          meltwater_seen: dict | None = None, month: str | None = None,
          map_days: int | None = None, now: datetime | None = None) -> dict:
    ctx: dict = {"colors": COLORS, "source_colors": SOURCE_COLORS, "source_labels": SOURCE_LABELS,
                 "signal_colors": SIGNAL_COLORS, "has_data": not posts.empty}
    ctx["alerts"] = build_alerts(alerts, meltwater_seen or {})
    try:
        from . import feedback as fb
        ctx["relevance_labels"] = {f"{t}:{i}": lab for (t, i), lab in fb.all_labels().items()}
        ctx["noisy_terms"] = fb.noisy_terms()
    except Exception:
        ctx["relevance_labels"] = {}
        ctx["noisy_terms"] = []
    ctx.setdefault("threat_chart", {"rows": [], "grouping": "", "unspecified": 0, "stored_types": [], "stored_targets": []})
    ctx.setdefault("protest_chart", {"rows": [], "grouping": "", "unspecified": 0, "stored_types": [], "stored_targets": []})
    ctx.setdefault("threat_topics", [])
    ctx.setdefault("protest_topics", [])
    ctx.setdefault("threat_authors", [])
    ctx.setdefault("protest_authors", [])
    ctx.setdefault("flagged_sample", [])
    ctx.setdefault("protest_posts", [])
    ctx.setdefault("posters", [])
    ctx.setdefault("upcoming", [])
    ctx.setdefault("narrative", "")
    from . import geo
    ctx["land"] = geo.australia_land()
    ctx["window_days"] = WINDOW_DAYS
    ctx["map"] = {"assets": geo.asset_markers(), "days": WINDOW_DAYS,
                 "colors": {"asset": geo.ASSET_COLOR, "past": geo.EVENT_COLOR,
                            "planned": geo.UPCOMING_COLOR,
                            "threat": geo.THREAT_MAP_COLOR},
                 "legend": [
                     {"label": "Sites", "color": geo.ASSET_COLOR, "swatch": "site"},
                     {"label": "Threats", "color": geo.THREAT_MAP_COLOR, "swatch": "fill"},
                     {"label": "Past protests", "color": geo.EVENT_COLOR, "swatch": "fill"},
                     {"label": "Planned protests", "color": geo.UPCOMING_COLOR, "swatch": "hollow"},
                 ]}
    ctx["upcoming"] = _upcoming(posts, alerts, now=now)
    if posts.empty:
        ctx["narrative"] = _narrative({"threats": 0, "protests": 0}, ctx["threat_chart"], ctx["upcoming"])
        return ctx
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    start = _window_start(moment)
    from .filters import Patterns
    from .judgment import display_flags, stated_targets
    from .textnorm import row_datetime, storage_dedupe_key
    patterns = Patterns()
    df = posts.copy()
    df["category"] = df.get("category", pd.Series("", index=df.index)).fillna("")
    df["eng"] = _num(df[_col(df, "engagement")]) if _col(df, "engagement") else 0
    flags, in_window, bodies = [], [], []
    seen_keys = set()
    keep_duplicate = []
    for _, row in df.iterrows():
        rec = row.to_dict()
        flagged = display_flags(rec, patterns)
        flags.append(flagged)
        when = row_datetime(rec)
        in_window.append(when is not None and when >= start)
        bodies.append(flagged.get("text") or "")
        key = storage_dedupe_key(rec)
        dup = bool(rec.get("duplicate_of")) or (key is not None and key in seen_keys)
        if key is not None and not rec.get("duplicate_of"):
            seen_keys.add(key)
        keep_duplicate.append(dup)
    df["_threat"] = [f["threat"] for f in flags]
    df["_protest"] = [f["protest"] for f in flags]
    df["_in_window"] = in_window
    df["_dup"] = keep_duplicate
    df["_body"] = bodies
    target_lists = []
    for i in range(len(df)):
        rec = df.iloc[i].to_dict()
        labels = stated_targets(rec, flags[i]["raw"])
        if not labels and flags[i]["protest"] and not flags[i]["threat"]:
            loc = str(rec.get("protest_location") or "").strip()
            if loc and loc.lower() not in {"unknown", "none", "n/a", "australia"}:
                labels = [loc]
        target_lists.append(labels)
    df["_targets"] = target_lists
    view = df[df["_in_window"] & ~df["_dup"]].copy()
    ctx["stored_posts"] = int(len(df))
    ctx["has_data"] = not view.empty
    t = view[view["_threat"]]
    p = view[view["_protest"]]
    heat_src = view
    heat = pd.to_numeric(heat_src.get("heat_score"), errors="coerce") if "heat_score" in heat_src else pd.Series(dtype=float)
    s_cur = {"threats": int(len(t)), "protests": int(len(p)),
             "confirmed_threats": int(t["is_genuine"].map(lambda v: v is True or str(v).lower() == "true").sum()) if "is_genuine" in t else 0,
             "confirmed_protests": int(p["is_genuine"].map(lambda v: v is True or str(v).lower() == "true").sum()) if "is_genuine" in p else 0,
             "avg_heat": round(float(heat.mean()), 1) if heat.notna().any() else None,
             "heat_posts": int(heat.notna().sum())}
    ctx["headline"] = [
        {"label": "Threat posts", "value": s_cur["threats"], "color": COLORS["threat"]},
        {"label": "Protest posts", "value": s_cur["protests"], "color": COLORS["protest"]},
        {"label": "Average heat (0 to 10)", "value": s_cur["avg_heat"] if s_cur["avg_heat"] is not None else "n/a",
         "color": "#e6e9ef"},
    ]
    ctx["volume"] = []
    ctx["threat_types"] = []
    ctx["targets"] = _split_counts(t["_targets"].map(lambda xs: "; ".join(xs)) if not t.empty else pd.Series(dtype=object),
                                   t["eng"] if not t.empty else pd.Series(dtype=float))
    ctx["threat_chart"] = build_threat_chart(t, "threat")
    ctx["narrative"] = _narrative(s_cur, ctx["threat_chart"], ctx["upcoming"])
    both = view[view["_threat"] | view["_protest"]]
    ctx["people"] = _split_counts(both.get("named_individuals", pd.Series(dtype=object)), both["eng"]) if not both.empty else []
    ctx["threat_topics"] = _named_counts(t, "threat_type")
    ctx["protest_topics"] = _named_counts(p, "protest_topic")
    ctx["protest_chart"] = build_threat_chart(p, "protest")
    threat_authors = _author_rank(t)
    protest_authors = _author_rank(p)
    ctx["threat_authors"] = threat_authors
    ctx["protest_authors"] = protest_authors
    ctx["posters"] = threat_authors
    ranked = t.sort_values("eng", ascending=False) if not t.empty else t
    ctx["flagged_sample"] = [_present(row) for _, row in ranked.head(25).iterrows()]
    protest_ranked = p.sort_values("eng", ascending=False) if not p.empty else p
    ctx["protest_posts"] = [_present(row) for _, row in protest_ranked.head(25).iterrows()]
    ctx["protest_locations"] = _split_counts(p.get("protest_location", pd.Series(dtype=object)), p["eng"]) if not p.empty else []
    ctx["organizers"] = _split_counts(p.get("organizers_or_groups", pd.Series(dtype=object)), p["eng"]) if not p.empty else []
    heat_rows = heat_src[pd.to_numeric(heat_src.get("heat_score"), errors="coerce").notna()] if "heat_score" in heat_src else heat_src.iloc[0:0]
    if not heat_rows.empty and "heat_topic" in heat_rows:
        h = heat_rows.assign(hs=pd.to_numeric(heat_rows["heat_score"], errors="coerce"))
        h = h[h["heat_topic"].fillna("").ne("")]
        g = (h.groupby("heat_topic").agg(avg=("hs", "mean"), total=("hs", "sum"), posts=("hs", "size"),
                                          eng=("eng", "sum")).reset_index()
             .sort_values(["avg", "total"], ascending=False).head(TOP_N))
        ctx["heat"] = [{"label": r.heat_topic.capitalize(), "avg": round(r.avg, 1), "total": int(r.total),
                        "posts": int(r.posts), "engagement": int(r.eng), "pct": round(10 * r.avg, 1)}
                       for r in g.itertuples()]
    else:
        ctx["heat"] = []
    ctx["summary"] = _summaries(ctx, s_cur, None)
    return ctx


def _present(row) -> dict:
    from .origin import origin_label
    from .textnorm import display_handle, first_source_url, format_row_when
    rec = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    text = str(rec.get("_body") or "")
    url = first_source_url(rec)
    platform = origin_label({**rec, "link": url, "url": url})
    if not platform or platform.lower() in {"unknown", "unknown source"}:
        platform = "Platform not supplied"
    tt = str(rec.get("threat_type") or "").strip()
    if tt.lower() in _HIDE_TYPE:
        tt = ""
    long = len(text) > 360
    return {
        "doc_id": rec.get("document_id") or rec.get("Document ID") or "",
        "text": text,
        "preview": (text[:360].rstrip() + "…") if long else text,
        "long": long,
        "type": tt,
        "link": url,
        "handle": display_handle(rec),
        "platform": platform,
        "when": format_row_when(rec),
        "source": platform,
    }


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



def _named_counts(frame: pd.DataFrame, column: str) -> list[dict]:
    """Rank one stored label per post. Blank and non-labels are left out."""
    if frame is None or frame.empty or column not in frame.columns:
        return []
    labels = frame[column].map(lambda v: "" if str(v or "").strip().lower() in _HIDE_TYPE else str(v or "").strip())
    return _split_counts(labels, frame["eng"] if "eng" in frame.columns else None)


def _author_rank(frame: pd.DataFrame) -> list[dict]:
    """Top accounts by distinct posts. The post text is not repeated here."""
    if frame is None or frame.empty:
        return []
    ranked = recurring_threat_posters(frame, min_count=1, respect_genuine=False, category_filter=False)
    return [{"handle": r.author_handle, "name": r.author_name, "count": int(r.threat_posts),
             "engagement": int(r.total_engagement)} for r in ranked.head(TOP_N).itertuples()]


def build_threat_chart(df: pd.DataFrame, noun: str = "threat") -> dict:
    """One bar per stated target. The same target is not split by type.

    Posts with no stated target are counted beside the chart, not ranked as a target.
    """
    empty = {"rows": [], "grouping": f"No stated {noun} targets in the last 30 days.",
             "unspecified": 0, "stored_types": [], "stored_targets": []}
    if df is None or df.empty:
        return empty
    from .judgment import stated_targets
    bag: dict[str, dict] = {}
    unspecified = 0
    eng = df["eng"] if "eng" in df.columns else (
        _num(df[_col(df, "engagement")]) if _col(df, "engagement") else pd.Series(0, index=df.index))
    for i, r in df.iterrows():
        labels = r.get("_targets") if isinstance(r.get("_targets"), list) else None
        if labels is None:
            labels = stated_targets(r.to_dict())
        weight = float(eng.get(i, 0) if hasattr(eng, "get") else 0)
        if not labels:
            unspecified += 1
            continue
        for label in labels:
            rec = bag.setdefault(label.lower(), {"label": label, "value": 0, "engagement": 0.0})
            rec["value"] += 1
            rec["engagement"] += weight
    if not bag and not unspecified:
        return empty
    items = sorted(bag.values(), key=lambda x: (x["value"], x["engagement"]), reverse=True)[:12]
    top = max((it["value"] for it in items), default=1) or 1
    for it in items:
        it["pct"] = round(100 * it["value"] / top, 1)
        it["engagement"] = int(it["engagement"])
        it["type"] = ""
    grouping = (
        "Each bar is one stated target. Names for the same target are combined, "
        "including AUKUS and AUKUS program, and the Australian government with the "
        "Albanese government, the Australian Labor government, and the Labor Party when "
        "that is the stated target. US bases stay separate. A post counts once for each "
        "of its targets. Posts with no stated target are not ranked"
        + (f" ({unspecified} {noun} post{'s' if unspecified != 1 else ''} in this window)."
           if unspecified else ".")
    )
    return {"rows": items, "grouping": grouping, "unspecified": unspecified,
            "stored_types": [], "stored_targets": [it["label"] for it in items]}


def _upcoming(posts: pd.DataFrame, alerts: pd.DataFrame | None, now: datetime | None = None) -> list[dict]:
    """Planned protests from stored dates. Uses local place names only (no network)."""
    from . import geo
    from .judgment import display_flags
    from .mapdata import _event_kind, _parse_date, _places_from_alert, _s, _split_places
    from .origin import origin_label
    from .textnorm import first_source_url, single_post_text, usable_source_url
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = []

    def keep(place: str) -> bool:
        hit = geo._centroid(place)
        if hit is None:
            return True
        return geo.in_australia(hit[0], hit[1])

    if posts is not None and not posts.empty:
        for r in posts.to_dict(orient="records"):
            data = r.get("data") if isinstance(r.get("data"), dict) else {}
            merged = {**data, **{k: v for k, v in r.items() if k != "data" and _s(v)}}
            flags = display_flags(merged)
            if not flags["protest"]:
                continue
            raw = flags["raw"].get("event_date") or merged.get("event_date") or ""
            when = _parse_date(str(raw)) or _parse_date(str(merged.get("narrative_summary") or "")) or _parse_date(flags.get("text") or "")
            if not when or when.date() <= now.date():
                continue
            status = str(merged.get("mobilization_status") or "")
            summary = single_post_text(merged) or _s(merged.get("narrative_summary"))
            if _event_kind(status, True, str(merged.get("refined_category") or ""), summary) != "planned":
                continue
            link = first_source_url(merged)
            places = _split_places(merged.get("protest_location")) or _split_places(flags["raw"].get("protest_location"))
            for place in places:
                if not keep(place):
                    continue
                rows.append({"date": when.strftime("%Y-%m-%d"), "place": place,
                             "source": origin_label(merged), "text": summary[:180], "url": link})
    if alerts is not None and not getattr(alerts, "empty", True):
        for r in alerts.to_dict(orient="records"):
            when = _parse_date(str(r.get("text") or ""))
            if not when or when.date() <= now.date():
                continue
            if _event_kind("", True, "", str(r.get("text") or "")) != "planned":
                continue
            for place in _places_from_alert(r):
                if not keep(place):
                    continue
                rows.append({"date": when.strftime("%Y-%m-%d"), "place": place,
                             "source": SOURCE_LABELS.get((r.get("source") or "").lower(), r.get("source") or ""),
                             "text": str(r.get("text") or "")[:180],
                             "url": usable_source_url(r.get("url") or "")})
    rows.sort(key=lambda e: (e.get("date") or "9999", e.get("place") or ""))
    return rows[:12]


def _narrative(current: dict, ranking: dict, upcoming: list) -> str:
    threats = current.get("threats") or 0
    protests = current.get("protests") or 0
    lead = ""
    rows = (ranking or {}).get("rows") or []
    if rows:
        top = rows[0]
        lead = f" The highest-ranked target is {top['label']}."
    unspecified = (ranking or {}).get("unspecified") or 0
    if unspecified:
        lead += f" {unspecified} threat post{'s' if unspecified != 1 else ''} ha{'ve' if unspecified != 1 else 's'} no stated target."
    planned = len(upcoming or [])
    plan = (f" {planned} planned protest{'s' if planned != 1 else ''} "
            f"{'are' if planned != 1 else 'is'} listed.")
    return (f"{threats} threat post{'s' if threats != 1 else ''} and "
            f"{protests} protest post{'s' if protests != 1 else ''} in the last {WINDOW_DAYS} days.{lead}{plan}")


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
