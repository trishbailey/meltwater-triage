"""Skip Claude for posts already classified in Postgres (or earlier in the same file).

Match order: Meltwater Document ID, then normalized URL/link, then normalized-text
fingerprint (covers retweets/reposts of the same wording). Dedup happens BEFORE any
Claude refine or heat call; saved classifications are copied onto the new rows.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

import pandas as pd

from .refine import REFINE_COLUMNS
from .storage import _doc_id, _lc

HEAT_REUSE_COLUMNS = ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled"]
_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


def normalize_text(text: str) -> str:
    text = _URL.sub(" ", (text or "").lower())
    text = _WS.sub(" ", text).strip()
    return text


def text_fingerprint(text: str) -> str:
    norm = normalize_text(text)
    if len(norm) < 24:
        return ""
    return "fp:" + hashlib.sha256(norm.encode("utf-8", "ignore")).hexdigest()[:32]


def _url_key(row: dict) -> str:
    for name in ("url", "link", "original url", "source url"):
        v = _lc(row, name)
        if v:
            try:
                from .alerts import normalize_url
                return normalize_url(str(v))
            except Exception:
                return str(v).strip().lower()[:500]
    return ""


def _row_text(row: dict, text_cols: list) -> str:
    parts = []
    for c in text_cols:
        if c in row and row[c] is not None and not (isinstance(row[c], float) and pd.isna(row[c])):
            parts.append(str(row[c]))
    if not parts:
        for name in ("hit sentence", "content", "text", "snippet"):
            v = _lc(row, name)
            if v:
                parts.append(str(v))
    return " ".join(parts)


def row_keys(row: dict, text_cols: list) -> tuple[str, str, str]:
    """Return (document_id, url_key, text_fingerprint)."""
    data = {str(k): v for k, v in row.items()}
    return _doc_id(data), _url_key(data), text_fingerprint(_row_text(data, text_cols))


def _payload_from_stored(doc_id: str, category, refined_category, severity, threat_type,
                         heat_score, heat_topic, data: dict | None) -> dict:
    data = data if isinstance(data, dict) else {}
    out = {"document_id": doc_id, "category": category or data.get("category") or ""}
    for col in REFINE_COLUMNS:
        val = data.get(col)
        if val in (None, "") and col == "refined_category":
            val = refined_category
        if val in (None, "") and col == "severity":
            val = severity
        if val in (None, "") and col == "threat_type":
            val = threat_type
        if val is not None and val != "":
            out[col] = val
    if heat_score is not None or data.get("heat_score") is not None:
        out["heat_score"] = heat_score if heat_score is not None else data.get("heat_score")
        out["heat_topic"] = heat_topic or data.get("heat_topic") or ""
        out["heat_subtopic"] = data.get("heat_subtopic") or ""
        out["heated_phrases"] = data.get("heated_phrases") or ""
        out["heat_sampled"] = True
    return out


def has_refine(payload: dict) -> bool:
    return bool(payload.get("refined_category") or payload.get("narrative_summary")
                or payload.get("is_genuine") is not None)


def has_heat(payload: dict) -> bool:
    return payload.get("heat_score") is not None or payload.get("heat_sampled") is True


class ReuseIndex:
    """In-memory indexes built from Storage.reuse_index() or empty."""

    def __init__(self):
        self.by_doc: dict[str, dict] = {}
        self.by_url: dict[str, dict] = {}
        self.by_fp: dict[str, dict] = {}

    def add(self, payload: dict, text_cols: list | None = None) -> None:
        doc = payload.get("document_id") or ""
        if doc:
            self.by_doc[doc] = payload
        url = payload.get("_url_key") or _url_key(payload)
        if url:
            self.by_url.setdefault(url, payload)
        fp = payload.get("_fp") or ""
        if not fp and text_cols is not None:
            fp = text_fingerprint(_row_text(payload, text_cols))
        if fp:
            self.by_fp.setdefault(fp, payload)

    def lookup(self, doc: str, url: str, fp: str) -> dict | None:
        if doc and doc in self.by_doc:
            return self.by_doc[doc]
        if url and url in self.by_url:
            return self.by_url[url]
        if fp and fp in self.by_fp:
            return self.by_fp[fp]
        return None


def apply_fields(dest: dict, src: dict, columns: list[str]) -> None:
    for col in columns:
        if col in src and src[col] is not None:
            dest[col] = src[col]


def split_for_claude(df: pd.DataFrame, text_cols: list, index: ReuseIndex,
                     *, kind: str) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Return (needs_claude, reused_rows_with_fields, skip_count).

    `kind` is "refine" or "heat". Reused rows already have the relevant columns filled.
    Within this dataframe, later rows that match an earlier new row's fingerprint also
    reuse that earlier classification once Claude fills it — handled by the caller via
    `remember_batch`.
    """
    if df is None or df.empty:
        empty = df.copy() if df is not None else pd.DataFrame()
        return empty, empty, 0
    cols = REFINE_COLUMNS if kind == "refine" else HEAT_REUSE_COLUMNS
    need_idx = []
    reuse_rows = []
    for i, row in df.iterrows():
        rec = row.to_dict()
        doc, url, fp = row_keys(rec, text_cols)
        hit = index.lookup(doc, url, fp)
        ok = hit and (has_refine(hit) if kind == "refine" else has_heat(hit))
        if ok:
            applied = rec.copy()
            apply_fields(applied, hit, cols)
            if kind == "heat":
                applied["heat_sampled"] = True
            reuse_rows.append(applied)
        else:
            need_idx.append(i)
    reused = pd.DataFrame(reuse_rows) if reuse_rows else df.iloc[0:0].copy()
    needs = df.loc[need_idx].copy() if need_idx else df.iloc[0:0].copy()
    return needs, reused, len(reuse_rows)


def remember_batch(index: ReuseIndex, rows: pd.DataFrame, text_cols: list, *, kind: str) -> None:
    """After Claude returns, index new classifications so same-file retweets can reuse them."""
    cols = REFINE_COLUMNS if kind == "refine" else HEAT_REUSE_COLUMNS
    if rows is None or rows.empty:
        return
    for _, row in rows.iterrows():
        rec = row.to_dict()
        doc, url, fp = row_keys(rec, text_cols)
        payload = {c: rec.get(c) for c in cols if c in rec}
        payload["document_id"] = doc
        payload["_url_key"] = url
        payload["_fp"] = fp
        if kind == "refine" and not has_refine(payload):
            continue
        if kind == "heat" and not has_heat(payload):
            continue
        index.add(payload)
