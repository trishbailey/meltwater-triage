"""Aggregations for reports and the future dashboard."""
from __future__ import annotations

import pandas as pd


def _col(df: pd.DataFrame, name: str):
    for c in df.columns:
        if str(c).strip().lower() == name:
            return c
    return None


def _engagement(df: pd.DataFrame) -> pd.Series:
    c = _col(df, "engagement")
    return pd.to_numeric(df[c], errors="coerce").fillna(0) if c else pd.Series(0, index=df.index)


def _post_dates(df: pd.DataFrame) -> pd.Series:
    c = _col(df, "date")
    return pd.to_datetime(df[c], errors="coerce") if c else pd.Series(pd.NaT, index=df.index)


def _distinct_threats(texts: list[str]) -> int:
    """Identical text is one threat. Empty text cannot be proven duplicate, so each such row counts."""
    filled = [t for t in texts if t]
    blanks = len(texts) - len(filled)
    if not filled:
        return len(texts)
    return len(set(filled)) + blanks


def recurring_threat_posters(flagged: pd.DataFrame, min_count: int = 2,
                             respect_genuine: bool = True, category_filter: bool = True) -> pd.DataFrame:
    """Authors with >= min_count distinct posts: handle, name, count, engagement, first/last seen.

    By default only threat rows count. Pass category_filter=False when the caller already
    chose the rows (threats or protests).
    """
    cols = ["author_handle", "author_name", "threat_posts", "total_engagement", "first_seen", "last_seen"]
    if flagged is None or flagged.empty:
        return pd.DataFrame(columns=cols)
    if category_filter:
        if "category" not in flagged:
            return pd.DataFrame(columns=cols)
        t = flagged[flagged["category"].isin(["threat", "both"])]
    else:
        t = flagged
    if respect_genuine and "is_genuine" in t:  # drop posts the review marked as not genuine
        t = t[t["is_genuine"].ne(False)]
    if t.empty:
        return pd.DataFrame(columns=cols)
    from .textnorm import display_handle, single_post_text, supplied_handle
    hc, nc = _col(t, "author handle"), _col(t, "author name")
    norms, handles, names = [], [], []
    for _, row in t.iterrows():
        rec = row.to_dict()
        norms.append(" ".join(single_post_text(rec).split()).strip().lower())
        raw_handle = supplied_handle(rec.get(hc)) if hc else ""
        raw_name = supplied_handle(rec.get(nc)) if nc else ""
        ident = display_handle(rec)
        if raw_handle:
            handles.append(raw_handle if raw_handle.startswith("@") else raw_handle)
        elif ident.startswith("@"):
            handles.append(ident)
        else:
            handles.append("")
        names.append(raw_name or ("" if ident.startswith("@") else ident))
    work = pd.DataFrame({
        "author_handle": handles,
        "author_name": names,
        "eng": _engagement(t).to_numpy(), "dt": _post_dates(t).to_numpy(),
        "norm": norms,
    })
    work["key"] = work["author_handle"].where(work["author_handle"] != "", work["author_name"]).str.lower()
    work = work[work["key"] != ""]
    rows = []
    for _, part in work.groupby("key", sort=False):
        count = _distinct_threats(part["norm"].tolist())
        if count < min_count:
            continue
        rows.append({
            "author_handle": part["author_handle"].iloc[0],
            "author_name": part["author_name"].iloc[0],
            "threat_posts": count,
            "total_engagement": float(part["eng"].sum()),
            "first_seen": part["dt"].min(),
            "last_seen": part["dt"].max(),
        })
    if not rows:
        return pd.DataFrame(columns=cols)
    g = pd.DataFrame(rows)
    return g.sort_values(["threat_posts", "total_engagement"], ascending=False).reset_index(drop=True)[cols]


def heat_by_topic(scored: pd.DataFrame) -> pd.DataFrame:
    """Topics ranked by average and total heat, with post count and engagement."""
    cols = ["topic", "posts", "avg_heat", "total_heat", "max_heat", "total_engagement",
            "avg_engagement"]
    if scored.empty or "heat_score" not in scored:
        return pd.DataFrame(columns=cols)
    s = scored[scored["heat_score"].notna() & scored["heat_topic"].fillna("").ne("")]
    if s.empty:
        return pd.DataFrame(columns=cols)
    work = pd.DataFrame({"topic": s["heat_topic"], "heat": s["heat_score"].astype(float),
                         "eng": _engagement(s)})
    g = work.groupby("topic").agg(posts=("heat", "size"), avg_heat=("heat", "mean"),
                                  total_heat=("heat", "sum"), max_heat=("heat", "max"),
                                  total_engagement=("eng", "sum"), avg_engagement=("eng", "mean"))
    g["avg_heat"] = g["avg_heat"].round(2)
    g["avg_engagement"] = g["avg_engagement"].round(1)
    return g.sort_values(["avg_heat", "total_heat"], ascending=False).reset_index()[cols]
