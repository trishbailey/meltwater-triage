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
# Highest number of screened rows held at once during the latest _screen_files call.
_peak_retained = {"n": 0}


class SaveFailed(RuntimeError):
    """Review may be checkpointed, but the dashboard save did not commit."""


@dataclass
class Result:
    flagged: pd.DataFrame
    all_posts: pd.DataFrame
    posters: pd.DataFrame
    topics: pd.DataFrame
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)
    screened_rows: int = 0
    saved: bool = True


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


def _keep_limit() -> int:
    cap = _heat_cap()
    try:
        return max(cap, int(os.environ.get("SCREEN_KEEP_ROWS", "2500")))
    except ValueError:
        return max(cap, 2500)


def _note_retained(n: int) -> None:
    if n > _peak_retained["n"]:
        _peak_retained["n"] = n


def _engagement(df: pd.DataFrame) -> pd.Series:
    eng_col = next((c for c in df.columns if str(c).strip().lower() == "engagement"), None)
    if eng_col is None:
        return pd.Series(0, index=df.index)
    return pd.to_numeric(df[eng_col], errors="coerce").fillna(0)


def _shrink_unflagged(frames: list[pd.DataFrame], limit: int) -> tuple[list[pd.DataFrame], int]:
    """Keep the highest-engagement unflagged rows only. Flagged rows are not in `frames`."""
    if limit <= 0 or not frames:
        return [], 0
    df = pd.concat(frames, ignore_index=True)
    if len(df) <= limit:
        return [df], len(df)
    idx = _engagement(df).sort_values(ascending=False, kind="stable").head(limit).index
    slim = df.loc[sorted(idx)].copy()
    return [slim], len(slim)


def _screen_files(files, report) -> tuple[pd.DataFrame, int]:
    """Screen chunk by chunk and drop unflagged rows once the file exceeds the keep limit.

    The running set is flagged rows plus a bounded engagement sample, not every
    screened row concatenated and pruned at the end. The returned count is still
    every row that was screened.
    """
    from .filters import Patterns
    patterns = Patterns()
    keep_under = _keep_limit()
    cap = _heat_cap()
    flagged_parts: list[pd.DataFrame] = []
    pool: list[pd.DataFrame] = []
    pool_n = 0
    total = 0
    _peak_retained["n"] = 0
    saw = False
    for raw in iter_upload_chunks(files):
        part = screen_dataframe(raw, patterns)
        saw = True
        total += len(part)
        _note_retained(sum(len(p) for p in flagged_parts) + pool_n + len(part))
        cat = part["category"].fillna("").astype(str)
        flagged = part.loc[cat != ""].copy()
        rest = part.loc[cat == ""].copy()
        if len(flagged):
            flagged_parts.append(flagged)
        if len(rest):
            pool.append(rest)
            pool_n += len(rest)
        columns = list(part.columns)
        del part, raw
        if total > keep_under and pool_n > cap:
            pool, pool_n = _shrink_unflagged(pool, cap)
        _note_retained(sum(len(p) for p in flagged_parts) + pool_n)
        report("Screening posts", total, 0)
    if not saw:
        raise ValueError("No non-empty worksheets found in the uploaded files.")
    frames = flagged_parts + pool
    screened = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)
    del frames, flagged_parts, pool
    screened[_ROW_ID] = range(len(screened))
    return screened, total


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
            progress=None, checkpoint=None) -> Result:
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

    def tick(stage, n_total, kind):
        def _add(n, records=None):
            # Commit finished classifications before any progress update for this batch.
            if records:
                payloads = [_checkpoint_payload(row, text_cols, kind) for row in records]
                payloads = [p for p in payloads if p]
                for payload in payloads:
                    index.add(payload)
                if checkpoint is not None and payloads:
                    checkpoint.save(kind, payloads)
            with lock:
                counter["done"] += n
                done = min(counter["done"], n_total)
            report(stage, done, n_total)
        return _add

    # Skip Claude for posts already in the database (doc id / URL / text fingerprint)
    # and for retweets of the same wording later in this file.
    index = ReuseIndex()
    if storage is not None and not storage_failed:
        try:
            index = storage.reuse_index(text_cols)
        except Exception as exc:
            log.warning("Could not load reuse index: %s", type(exc).__name__)
    if checkpoint is not None:
        try:
            for rec in checkpoint.load() or []:
                index.add(rec)
        except Exception as exc:
            log.warning("Could not load review checkpoint: %s", type(exc).__name__)
            raise

    refine_need, refine_reused, refine_skipped = reuse_mod.split_for_claude(
        flagged, text_cols, index, kind="refine")
    refine_need, refine_deferred = _collapse_same_keys(refine_need, text_cols)
    counter["done"] = 0
    report("Reviewing flagged posts", 0, len(refine_need))
    refined_new = refine(refine_need, text_cols, complete=refine_fn, budget=budget,
                         progress=tick("Reviewing flagged posts", len(refine_need), "refine"))
    reuse_mod.remember_batch(index, refined_new, text_cols, kind="refine")
    refined_deferred = _fill_deferred(refine_deferred, refined_new, text_cols, kind="refine")
    same_file = 0 if refine_deferred is None else len(refine_deferred)
    flagged = _combine_parts(flagged, [refined_new, refine_reused, refined_deferred], REFINE_COLUMNS)

    heat_total = min(total, _heat_cap())
    heat_need, heat_reused, heat_skipped = reuse_mod.split_for_claude(
        screened, text_cols, index, kind="heat")
    heat_need, heat_deferred = _collapse_same_keys(heat_need, text_cols)
    counter["done"] = 0
    heat_report_n = min(len(heat_need), heat_total)
    report("Scoring language", 0, heat_report_n)
    heated_new = analyze_heat(heat_need, text_cols, complete=heat_fn, budget=budget,
                              max_posts=heat_total,
                              progress=tick("Scoring language", heat_report_n, "heat"))
    reuse_mod.remember_batch(index, heated_new, text_cols, kind="heat")
    heated_deferred = _fill_deferred(heat_deferred, heated_new, text_cols, kind="heat")
    heat_same = 0 if heat_deferred is None else len(heat_deferred)
    screened = _combine_parts(screened, [heated_new, heat_reused, heated_deferred],
                              ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled"])

    report("Saving for the dashboard", total, total)
    heat_cols = [c for c in ("heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled")
                 if c in screened.columns]
    if heat_cols and "heat_sampled" in screened.columns and screened["heat_sampled"].fillna(False).astype(bool).any():
        flagged = flagged.drop(columns=[c for c in heat_cols if c in flagged.columns], errors="ignore")
        flagged = flagged.merge(screened[[_ROW_ID, *heat_cols]], on=_ROW_ID, how="left")
    screened = _drop_row_id(screened)
    flagged = _drop_row_id(flagged).reset_index(drop=True)
    from .judgment import apply_judgment_df
    from .textnorm import dedupe_posts
    flagged = dedupe_posts(apply_judgment_df(flagged))
    res = Result(flagged=flagged, all_posts=screened,
                 posters=recurring_threat_posters(flagged), topics=heat_by_topic(screened),
                 screened_rows=total)
    if same_file:
        res.notes.append(
            f"Reused one review for {same_file} same-file repost"
            f"{'' if same_file == 1 else 's'} with the same text.")
    if heat_same:
        res.notes.append(
            f"Reused one heat score for {heat_same} same-file duplicate"
            f"{'' if heat_same == 1 else 's'} (document id, URL, or same text).")
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
        except Exception as exc:
            log.error("Saving run failed: %s", type(exc).__name__)
            res.saved = False
            res.notes.append("Results were not saved to the database. Completed reviews were kept.")
            return res
    return res


def _collapse_same_keys(df: pd.DataFrame, text_cols: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Keep one row per document id, URL, or text fingerprint. Copy the rest after."""
    if df is None or df.empty:
        empty = df.copy() if df is not None else pd.DataFrame()
        return empty, empty
    keep, defer, seen = [], [], set()
    for i, row in df.iterrows():
        doc, url, fp = reuse_mod.row_keys(row.to_dict(), text_cols)
        keys = [k for k in (f"d:{doc}" if doc else "", f"u:{url}" if url else "", f"f:{fp}" if fp else "") if k]
        if keys and any(k in seen for k in keys):
            defer.append(i)
        else:
            seen.update(keys)
            keep.append(i)
    return df.loc[keep].copy(), df.loc[defer].copy()


def _checkpoint_payload(row: dict, text_cols: list, kind: str) -> dict | None:
    doc, url, fp = reuse_mod.row_keys(row, text_cols)
    cols = REFINE_COLUMNS if kind == "refine" else reuse_mod.HEAT_REUSE_COLUMNS
    payload = {"document_id": doc, "_url_key": url, "_fp": fp, "kind": kind}
    kept = False
    for col in cols:
        if col not in row:
            continue
        val = row.get(col)
        try:
            if val is None or pd.isna(val):
                continue
        except (TypeError, ValueError):
            pass
        if hasattr(val, "item"):
            try:
                val = val.item()
            except Exception:
                val = str(val)
        payload[col] = val
        kept = True
    if kind == "heat":
        payload["heat_sampled"] = True
        kept = True
    if not kept:
        return None
    return payload


def _fill_deferred(deferred: pd.DataFrame, classified: pd.DataFrame, text_cols: list, *, kind: str) -> pd.DataFrame:
    if deferred is None or deferred.empty:
        return deferred.copy() if deferred is not None else pd.DataFrame()
    cols = (REFINE_COLUMNS if kind == "refine" else
            ["heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled"])
    by_doc, by_url, by_fp = {}, {}, {}
    if classified is not None and not classified.empty:
        for _, row in classified.iterrows():
            rec = row.to_dict()
            doc, url, fp = reuse_mod.row_keys(rec, text_cols)
            if doc:
                by_doc[doc] = rec
            if url:
                by_url[url] = rec
            if fp:
                by_fp[fp] = rec
    out = []
    for _, row in deferred.iterrows():
        rec = row.to_dict()
        doc, url, fp = reuse_mod.row_keys(rec, text_cols)
        src = (by_doc.get(doc) if doc else None) or (by_url.get(url) if url else None) or (by_fp.get(fp) if fp else None)
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
