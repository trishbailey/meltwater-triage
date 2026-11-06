"""Excel sheets of pre-sorted posts with the AI review beside the original export columns.

Used by the upload download and by the dashboard download buttons, so a dashboard user gets
the raw posts and the AI analysis together and does not have to run the review again.
"""
from __future__ import annotations

import pandas as pd

from .origin import origin_label

THREAT_CATS = {"threat", "both"}
PROTEST_CATS = {"protest", "both"}
_TEXT_ORDER = ("hit sentence", "opening text", "title", "post text", "text", "content",
               "message", "tweet", "full text", "comment")
_THREAT_AI = ["severity", "threat_type", "theme", "targets", "named_individuals"]
_PROTEST_AI = ["protest_topic", "protest_location", "organizers_or_groups", "event_date",
               "scale_or_turnout", "targets"]
_SHARED_AI = ["is_genuine", "narrative_summary"]
_FLAG = ["category", "matched_keywords", "mobilization_status"]
_INTERNAL = {"item_type", "item_id", "is_flagged", "post_week", "post_month", "upload_week", "upload_month", "post_date",
             "origin_source", "publisher", "refined_category", "document_id"}


def _lookup(rec: dict, name: str):
    for k, v in rec.items():
        if str(k).strip().lower() == name:
            return v
    return None


def _blank(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v)) or str(v).strip() == ""
    except (TypeError, ValueError):
        return False


def post_text(rec: dict) -> str:
    for name in _TEXT_ORDER:
        v = _lookup(rec, name)
        if not _blank(v):
            return str(v).strip()
    return ""


def author_handle(rec: dict) -> str:
    for name in ("author handle", "author name", "author"):
        v = _lookup(rec, name)
        if not _blank(v):
            return str(v).strip()
    return ""


def is_rejected(value) -> bool:
    return value is False or str(value).strip().lower() in {"false", "0", "0.0"}


def _frame(df: pd.DataFrame, ai_cols: list[str], labels: dict | None) -> pd.DataFrame:
    recs = df.to_dict(orient="records")
    rows = []
    for rec in recs:
        doc = rec.get("item_id") or rec.get("document_id") or _lookup(rec, "document id") or ""
        kind = rec.get("item_type") or "post"
        rows.append({
            "platform": rec.get("origin_source") or origin_label(rec),
            "author_handle": author_handle(rec),
            "author_name": _lookup(rec, "author name") or "",
            "date": _lookup(rec, "date") or "",
            "post_text": post_text(rec),
            "link": rec.get("link") or _lookup(rec, "url") or "",
            "engagement": _lookup(rec, "engagement"),
            "source_file": rec.get("export_source") or "",
            **{c: rec.get(c) for c in _SHARED_AI + ai_cols},
            **{c: rec.get(c) for c in _FLAG},
            "analyst_relevance": (labels or {}).get((kind, str(doc)), ""),
            "document_id": doc,
        })
    out = pd.DataFrame(rows)
    used = {"url", "link", "date", "engagement", "author handle", "author name"} | set(_TEXT_ORDER)
    extra = [c for c in df.columns
             if str(c).strip().lower() not in used and c not in out.columns and c not in _INTERNAL
             and c not in _SHARED_AI + _THREAT_AI + _PROTEST_AI + _FLAG]
    for c in extra:
        out[c] = df[c].to_numpy()
    sort = pd.to_datetime(out["date"], errors="coerce")
    eng = pd.to_numeric(out["engagement"], errors="coerce").fillna(0)
    order = pd.DataFrame({"d": sort, "e": eng}).sort_values(["d", "e"], ascending=False, na_position="last").index
    return out.loc[order].reset_index(drop=True)


def split_posts(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(threat, protest, rejected). A post marked both appears in each of the first two."""
    if df is None or df.empty or "category" not in df.columns:
        empty = pd.DataFrame()
        return empty, empty, empty
    cat = df["category"].fillna("").astype(str)
    genuine = df["is_genuine"] if "is_genuine" in df.columns else pd.Series(None, index=df.index)
    rejected = genuine.map(is_rejected)
    kept = df[~rejected]
    kcat = cat[~rejected]
    return (kept[kcat.isin(THREAT_CATS)], kept[kcat.isin(PROTEST_CATS)],
            df[rejected & cat.ne("")])


def post_sheets(df: pd.DataFrame, labels: dict | None = None) -> list[tuple[str, pd.DataFrame]]:
    """Threat posts and protest posts as separate sheets, plus any the AI review rejected."""
    threat, protest, rejected = split_posts(df)
    sheets = [("Threat posts", _frame(threat, _THREAT_AI, labels) if len(threat)
               else pd.DataFrame(columns=["platform", "author_handle", "date", "post_text", "link"])),
              ("Protest posts", _frame(protest, _PROTEST_AI, labels) if len(protest)
               else pd.DataFrame(columns=["platform", "author_handle", "date", "post_text", "link"]))]
    if len(rejected):
        sheets.append(("Rejected by AI review", _frame(rejected, _THREAT_AI + _PROTEST_AI, labels)))
    return sheets


def _is_relevant(rec: dict, labels: dict) -> bool:
    doc = rec.get("item_id") or rec.get("document_id") or _lookup(rec, "document id") or ""
    return (labels or {}).get((rec.get("item_type") or "post", str(doc))) == "relevant"


def single_sheet(df: pd.DataFrame, kind: str, labels: dict | None = None,
                 relevant_only: bool = False) -> list[tuple[str, pd.DataFrame]]:
    """One category for the dashboard download.

    With relevant_only, the sheet holds just the posts the analyst marked Relevant. Posts marked
    Not relevant, posts not yet reviewed, and posts the AI rejected are left out.
    """
    threat, protest, rejected = split_posts(df)
    posts = threat if kind == "threat" else protest
    cats = THREAT_CATS if kind == "threat" else PROTEST_CATS
    ai = _THREAT_AI if kind == "threat" else _PROTEST_AI
    name = "Threat posts" if kind == "threat" else "Protest posts"
    if relevant_only and len(posts):
        keep = [_is_relevant(rec, labels) for rec in posts.to_dict(orient="records")]
        posts = posts[keep]
    sheets = [(name, _frame(posts, ai, labels) if len(posts)
               else pd.DataFrame(columns=["platform", "author_handle", "date", "post_text", "link"]))]
    rej = rejected[rejected["category"].isin(cats)] if len(rejected) else rejected
    if len(rej) and not relevant_only:
        sheets.append(("Rejected by AI review", _frame(rej, ai, labels)))
    return sheets
