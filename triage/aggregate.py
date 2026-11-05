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


def recurring_threat_posters(flagged: pd.DataFrame, min_count: int = 2) -> pd.DataFrame:
    """Authors with >= min_count threat posts: handle, name, count, engagement, first/last seen."""
    cols = ["author_handle", "author_name", "threat_posts", "total_engagement", "first_seen", "last_seen"]
    if flagged.empty or "category" not in flagged:
        return pd.DataFrame(columns=cols)
    t = flagged[flagged["category"].isin(["threat", "both"])]
    if "is_genuine" in t:  # drop posts the review marked as not genuine
        t = t[t["is_genuine"].ne(False)]
    if t.empty:
        return pd.DataFrame(columns=cols)
    hc, nc = _col(t, "author handle"), _col(t, "author name")
    work = pd.DataFrame({
        "author_handle": t[hc].fillna("").astype(str).str.strip() if hc else "",
        "author_name": t[nc].fillna("").astype(str).str.strip() if nc else "",
        "eng": _engagement(t), "dt": _post_dates(t),
    })
    work["key"] = work["author_handle"].where(work["author_handle"] != "", work["author_name"]).str.lower()
    work = work[work["key"] != ""]
    g = work.groupby("key").agg(
        author_handle=("author_handle", "first"), author_name=("author_name", "first"),
        threat_posts=("key", "size"), total_engagement=("eng", "sum"),
        first_seen=("dt", "min"), last_seen=("dt", "max"))
    g = g[g["threat_posts"] >= min_count]
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
