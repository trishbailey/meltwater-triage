"""End-to-end processing of one upload: screen, review, heat, aggregate, persist."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

import pandas as pd

from . import keywords as kw
from .aggregate import heat_by_topic, recurring_threat_posters
from .budget import resolve_budget
from .excel_safe import write_sheets
from .filters import _file_label, _find_columns, iter_upload_chunks, screen_dataframe
from .heat import analyze_heat
from .refine import refine
from .storage import persistence_enabled
from . import reuse as reuse_mod
from .reuse import ReuseIndex
from .refine import REFINE_COLUMNS

log = logging.getLogger(__name__)

_ROW_ID = "__triage_src_row"


@dataclass
class Result:
    flagged: pd.DataFrame
    all_posts: pd.DataFrame
    posters: pd.DataFrame
    topics: pd.DataFrame
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)
    screened_rows: int = 0


def _open_storage(storage):
    """Return (storage, failed). Creates the configured database when storage is omitted."""
    if storage is not None or not persistence_enabled():
        return storage, False
    try:
        from .storage import Storage
        return Storage(), False
    except Exception as exc:
        log.error("Opening database failed: %s", type(exc).__name__)
        return None, True


def _heat_cap() -> int:
    try:
        return max(0, int(os.environ.get("HEAT_MAX_POSTS", "300")))
    except ValueError:
        return 300


def _drop_row_id(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=_ROW_ID) if _ROW_ID in df.columns else df


def _screen_files(files, report) -> tuple[pd.DataFrame, int]:
    """Screen in chunks so the raw sheet and a second full copy are not held together.

    When the file is larger than the heat cap (and SCREEN_KEEP_ROWS), rows that are
    neither flagged nor in the engagement sample are dropped before review. The
    returned count is still every row that was screened.
    """
    from .filters import Patterns
    patterns = Patterns()
    parts = []
    for raw in iter_upload_chunks(files):
        part = screen_dataframe(raw, patterns)
        parts.append(part)
        report("Screening posts", sum(len(p) for p in parts), 0)
        del raw
    if not parts:
        raise ValueError("No non-empty worksheets found in the uploaded files.")
    screened = pd.concat(parts, ignore_index=True)
    del parts
    screened[_ROW_ID] = range(len(screened))
    return _limit_working_set(screened)


def _limit_working_set(screened: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    total = len(screened)
    cap = _heat_cap()
    try:
        keep_under = max(cap, int(os.environ.get("SCREEN_KEEP_ROWS", "2500")))
    except ValueError:
        keep_under = max(cap, 2500)
    if total <= keep_under:
        return screened, total
    eng_col = next((c for c in screened.columns if str(c).strip().lower() == "engagement"), None)
    eng = (pd.to_numeric(screened[eng_col], errors="coerce").fillna(0) if eng_col is not None
           else pd.Series(0, index=screened.index))
    top = set(eng.sort_values(ascending=False, kind="stable").head(max(cap, 0)).index)
    flagged = set(screened.index[screened["category"].fillna("").astype(str) != ""])
    slim = screened.loc[screened.index.isin(top | flagged)].copy()
    return slim, total


def backfill_flagged(storage, complete=None) -> int:
    """Review flagged posts that have no stored review, and merge the result.

    Does not delete posts. Returns how many rows were updated. With no completion
    function and no API key, returns 0.
    """
    from .refine import is_enabled, refine
    posts = storage.load_posts(flagged_only=True)
    if posts.empty:
        return 0
    refined = posts["refined_category"] if "refined_category" in posts.columns else pd.Series("", index=posts.index)
    need = posts[refined.fillna("").astype(str).str.strip().eq("")].copy()
    if need.empty:
        return 0
    if complete is None and not is_enabled():
        return 0
    text_cols = _find_columns(need, kw.TEXT_COLUMN_OPTIONS)
    reviewed = refine(need, text_cols, complete=complete)
    updated = 0
    for rec in reviewed.to_dict(orient="records"):
        if not rec.get("refined_category") and rec.get("is_genuine") is None:
            continue
        if storage.merge_review(rec):
            updated += 1
    return updated


def process(files: list[tuple[str, bytes]], refine_fn=None, heat_fn=None, storage=None,
            progress=None) -> Result:
    """progress(stage, done, total) is called as posts move through the pipeline."""
    report = progress or (lambda *a: None)
    report("Reading file", 0, 0)
    screened, total_rows = _screen_files(files, report)
    text_cols = _find_columns(screened, kw.TEXT_COLUMN_OPTIONS)
    storage, storage_failed = _open_storage(storage)
    # Review spends the monthly allowance first; heat uses whatever remains.
    budget = resolve_budget(storage, open_storage=False)
    flagged = screened.loc[screened["category"] != ""].copy()
    total = total_rows
    report("Screening posts", total, total)
    counter = {"done": 0}
    lock = threading.Lock()

    def tick(stage, n_total):
        def _add(n):
            with lock:
                counter["done"] += n
                report(stage, min(counter["done"], n_total), n_total)
        return _add

    # Skip Claude for posts already in the database (doc id / URL / text fingerprint)
    # and for retweets of the same wording later in this file.
    index = ReuseIndex()
    if storage is not None and not storage_failed:
        try:
            index = storage.reuse_index(text_cols)
        except Exception as exc:
            log.warning("Could not load reuse index: %s", type(exc).__name__)

    refine_need, refine_reused, refine_skipped = reuse_mod.split_for_claude(
        flagged, text_cols, index, kind="refine")
    refine_need, refine_deferred = _collapse_same_text(refine_need, text_cols)
    counter["done"] = 0
    report("Reviewing flagged posts", 0, len(refine_need))
    refined_new = refine(refine_need, text_cols, complete=refine_fn, budget=budget,
                         progress=tick("Reviewing flagged posts", len(refine_need)))
    reuse_mod.remember_batch(index, refined_new, text_cols, kind="refine")
    refined_deferred = _fill_deferred(refine_deferred, refined_new, text_cols, kind="refine")
    same_file = 0 if refine_deferred is None else len(refine_deferred)
    flagged = _combine_parts(flagged, [refined_new, refine_reused, refined_deferred], REFINE_COLUMNS)

    heat_total = min(total, _heat_cap())
    heat_need, heat_reused, heat_skipped = reuse_mod.split_for_claude(
        screened, text_cols, index, kind="heat")
    # Only score posts that still need heat, respecting HEAT_MAX_POSTS on the need set.
    counter["done"] = 0
    report("Scoring language", 0, min(len(heat_need), heat_total))
    heated_new = analyze_heat(heat_need, text_cols, complete=heat_fn, budget=budget,
                              max_posts=heat_total,
                              progress=tick("Scoring language", min(len(heat_need), heat_total)))
    reuse_mod.remember_batch(index, heated_new, text_cols, kind="heat")
    screened = _combine_parts(screened, [heated_new, heat_reused],
                              ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled"])

    report("Saving for the dashboard", total, total)
    heat_cols = [c for c in ("heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled")
                 if c in screened.columns]
    if heat_cols and "heat_sampled" in screened.columns and screened["heat_sampled"].fillna(False).astype(bool).any():
        flagged = flagged.drop(columns=[c for c in heat_cols if c in flagged.columns], errors="ignore")
        flagged = flagged.merge(screened[[_ROW_ID, *heat_cols]], on=_ROW_ID, how="left")
    screened = _drop_row_id(screened)
    flagged = _drop_row_id(flagged).reset_index(drop=True)
    res = Result(flagged=flagged, all_posts=screened,
                 posters=recurring_threat_posters(flagged), topics=heat_by_topic(screened),
                 screened_rows=total)
    if same_file:
        res.notes.append(
            f"Reused one review for {same_file} same-file repost"
            f"{'' if same_file == 1 else 's'} with the same text.")
    if refine_skipped or heat_skipped:
        res.notes.append(
            f"Skipped a new review for {refine_skipped} already-classified flagged post"
            f"{'' if refine_skipped == 1 else 's'}"
            f" and {heat_skipped} already heat-scored post"
            f"{'' if heat_skipped == 1 else 's'} (matched by document id, URL, or same text).")
    note = budget.warning()
    if note:
        res.notes.append(note)
    if storage_failed:
        res.notes.append("Results were not saved to the database.")
    elif storage is not None:
        from .storage import rows_to_store
        try:
            res.run_id = storage.save_run(
                rows_to_store(screened, flagged), [_file_label(n) for n, _ in files], total)
        except Exception as exc:  # never lose the user's download because storage failed
            log.error("Saving run failed: %s", type(exc).__name__)
            res.notes.append("Results were not saved to the database.")
    return res


def _collapse_same_text(df: pd.DataFrame, text_cols: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep one row per text fingerprint for Claude; defer the rest to copy after."""
    if df is None or df.empty:
        empty = df.copy() if df is not None else pd.DataFrame()
        return empty, empty
    keep, defer, seen = [], [], set()
    for i, row in df.iterrows():
        _doc, _url, fp = reuse_mod.row_keys(row.to_dict(), text_cols)
        if fp and fp in seen:
            defer.append(i)
        else:
            if fp:
                seen.add(fp)
            keep.append(i)
    return df.loc[keep].copy(), df.loc[defer].copy()


def _fill_deferred(deferred: pd.DataFrame, classified: pd.DataFrame, text_cols: list, *, kind: str) -> pd.DataFrame:
    if deferred is None or deferred.empty:
        return deferred.copy() if deferred is not None else pd.DataFrame()
    cols = (REFINE_COLUMNS if kind == "refine" else
            ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled"])
    by_fp = {}
    for _, row in classified.iterrows():
        _d, _u, fp = reuse_mod.row_keys(row.to_dict(), text_cols)
        if fp:
            by_fp[fp] = row.to_dict()
    out = []
    for _, row in deferred.iterrows():
        rec = row.to_dict()
        _d, _u, fp = reuse_mod.row_keys(rec, text_cols)
        src = by_fp.get(fp)
        if src:
            reuse_mod.apply_fields(rec, src, cols)
            if kind == "heat":
                rec["heat_sampled"] = True
        out.append(rec)
    return pd.DataFrame(out) if out else deferred.iloc[0:0].copy()


def _combine_parts(original: pd.DataFrame, parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    """Merge Claude/reuse columns back onto the original row order via _ROW_ID."""
    out = original.copy()
    for col in columns:
        if col not in out.columns:
            out[col] = pd.NA if col != "heat_sampled" else False
    frames = [p for p in parts if p is not None and not p.empty and _ROW_ID in getattr(p, "columns", [])]
    if not frames:
        return out
    merged = pd.concat(frames, ignore_index=True, sort=False)
    # Last write wins; prefer non-null.
    merged = merged.drop_duplicates(subset=[_ROW_ID], keep="last")
    out = out.drop(columns=[c for c in columns if c in out.columns], errors="ignore")
    cols = [_ROW_ID] + [c for c in columns if c in merged.columns]
    return out.merge(merged[cols], on=_ROW_ID, how="left")


def to_workbook(res: Result) -> bytes:
    sheets: list[tuple[str, pd.DataFrame]] = []
    if res.notes:
        sheets.append(("Warnings", pd.DataFrame({"warning": res.notes})))
    sheets.append(("Flagged", res.flagged))
    sheets.append(("Recurring threat posters", res.posters))
    if "heat_score" in res.all_posts.columns and "heat_sampled" in res.all_posts.columns:
        sampled = res.all_posts[res.all_posts["heat_sampled"].fillna(False).astype(bool)]
        if not sampled.empty:
            sheets.append(("Heat by topic", res.topics))
            sheets.append(("Heat (all posts)", sampled))
    return write_sheets(sheets)
