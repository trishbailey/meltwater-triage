"""End-to-end processing of one upload.

1. Pre-sort: the code screens every row and keeps only viable threat and protest posts.
2. Save: the pre-sorted posts go to the dashboard right away, before any AI call.
3. Review: Claude evaluates only those flagged posts, and each finished batch is merged
   into the saved rows as it arrives.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

import pandas as pd

from . import keywords as kw
from .aggregate import recurring_threat_posters
from .budget import resolve_budget
from .excel_safe import write_sheets
from .filters import _file_label, _find_columns, iter_upload_chunks, screen_dataframe
from .refine import few_shot_block, refine
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
    posters: pd.DataFrame
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)
    screened_rows: int = 0
    saved: bool = True

    @property
    def all_posts(self) -> pd.DataFrame:
        """Only flagged posts are kept; unflagged rows are counted and dropped."""
        return self.flagged


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


def _drop_row_id(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=_ROW_ID) if _ROW_ID in df.columns else df


def _note_retained(n: int) -> None:
    if n > _peak_retained["n"]:
        _peak_retained["n"] = n


def _screen_files(files, report) -> tuple[pd.DataFrame, int]:
    """Pre-sort chunk by chunk and keep only flagged rows.

    Unflagged rows are counted and dropped as soon as their chunk is screened, so memory
    holds the viable threat and protest posts and one chunk. The returned count is every
    row that was screened.
    """
    from .filters import Patterns
    patterns = Patterns()
    parts: list[pd.DataFrame] = []
    kept = 0
    total = 0
    columns: list = []
    _peak_retained["n"] = 0
    saw = False
    for raw in iter_upload_chunks(files):
        part = screen_dataframe(raw, patterns)
        saw = True
        total += len(part)
        columns = list(part.columns)
        _note_retained(kept + len(part))
        flagged = part.loc[part["category"].fillna("").astype(str) != ""].copy()
        if len(flagged):
            parts.append(flagged)
            kept += len(flagged)
        del part, raw
        _note_retained(kept)
        report("Pre-sorting posts", total, 0)
    if not saw:
        raise ValueError("No non-empty worksheets found in the uploaded files.")
    screened = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)
    screened[_ROW_ID] = range(len(screened))
    return screened, total


def backfill_flagged(storage, complete=None) -> int:
    """Review flagged posts that have no stored review, and merge the result.

    Does not delete posts. Returns how many rows were updated. With no completion
    function and no API key, returns 0.
    """
    from .refine import is_enabled, refine
    from .unified import cutoff
    posts = storage.load_posts(flagged_only=True, since=cutoff())  # skip posts before the start date
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
    """progress(stage, done, total) is called as posts move through the pipeline.

    `heat_fn` is accepted and ignored: the language-heat stage that scored unflagged posts
    was removed, so Claude only ever sees posts the pre-sort flagged.
    """
    report = progress or (lambda *a: None)
    report("Reading file", 0, 0)
    flagged, total = _screen_files(files, report)
    report("Pre-sorting posts", total, total)
    text_cols = _find_columns(flagged, kw.TEXT_COLUMN_OPTIONS) if len(flagged.columns) else []
    storage, storage_failed = _open_storage(storage)
    budget = resolve_budget(storage, open_storage=False)
    labels = [_file_label(n) for n, _ in files]
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
                if storage is not None and not storage_failed and saved_ok["v"]:
                    try:  # the dashboard shows each batch as soon as it is reviewed
                        storage.merge_reviews(records)
                    except Exception as exc:
                        log.warning("Merging a reviewed batch failed: %s", type(exc).__name__)
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

    need, reused, skipped = reuse_mod.split_for_claude(flagged, text_cols, index, kind="refine")
    need, deferred = _collapse_same_keys(need, text_cols)

    # Save the pre-sorted posts first, so the dashboard has them before Claude starts.
    saved_ok = {"v": False}
    res = Result(flagged=flagged, posters=recurring_threat_posters(flagged), screened_rows=total)
    if storage_failed:
        res.notes.append("Results were not saved to the database.")
    elif storage is not None:
        from .storage import rows_to_store
        report("Saving for the dashboard", total, total)
        base = _combine_parts(flagged, [reused], REFINE_COLUMNS)
        try:
            res.run_id = storage.save_run(rows_to_store(base, _drop_row_id(base)), labels, total)
            saved_ok["v"] = True
        except Exception as exc:
            log.error("Saving run failed: %s", type(exc).__name__)
            res.saved = False
            res.notes.append("Results were not saved to the database. Nothing was sent for review.")
            res.flagged = _drop_row_id(flagged).reset_index(drop=True)
            return res

    counter["done"] = 0
    report("Reviewing flagged posts", 0, len(need))
    examples = few_shot_block(storage) if len(need) else ""
    refined_new = refine(need, text_cols, complete=refine_fn, budget=budget, examples=examples,
                         progress=tick("Reviewing flagged posts", len(need), "refine"))
    reuse_mod.remember_batch(index, refined_new, text_cols, kind="refine")
    refined_deferred = _fill_deferred(deferred, refined_new, text_cols, kind="refine")
    same_file = 0 if deferred is None else len(deferred)
    flagged = _combine_parts(flagged, [refined_new, reused, refined_deferred], REFINE_COLUMNS)
    if saved_ok["v"] and refined_deferred is not None and not refined_deferred.empty:
        try:
            storage.merge_reviews(refined_deferred.to_dict(orient="records"))
        except Exception as exc:
            log.warning("Merging same-file reposts failed: %s", type(exc).__name__)

    flagged = _drop_row_id(flagged).reset_index(drop=True)
    res.flagged = flagged
    res.posters = recurring_threat_posters(flagged)
    if same_file:
        res.notes.append(
            f"Reused one review for {same_file} same-file repost"
            f"{'' if same_file == 1 else 's'} with the same text.")
    if skipped:
        res.notes.append(
            f"Skipped a new review for {skipped} already-classified flagged post"
            f"{'' if skipped == 1 else 's'} (matched by document id, URL, or same text).")
    note = budget.warning()
    unreviewed = int((flagged["refined_category"].fillna("").astype(str) == "").sum()) \
        if "refined_category" in flagged.columns and len(flagged) else len(flagged)
    if note:
        res.notes.append(note)
    elif unreviewed and (refine_fn is not None or refine_mod_enabled()):
        res.notes.append(
            f"{unreviewed} flagged post{'' if unreviewed == 1 else 's'} did not get an AI review "
            "because a Claude request failed. They stay on the dashboard as pre-sorted posts, "
            "and the next upload of the same posts retries them.")
    return res


def refine_mod_enabled() -> bool:
    from .refine import is_enabled
    return is_enabled()


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
    from .export import post_sheets
    sheets: list[tuple[str, pd.DataFrame]] = []
    if res.notes:
        sheets.append(("Warnings", pd.DataFrame({"warning": res.notes})))
    sheets += post_sheets(res.flagged)
    if not res.posters.empty:
        sheets.append(("Recurring threat posters", res.posters))
    return write_sheets(sheets)
