"""End-to-end processing of one upload: screen, review, heat, aggregate, persist."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from . import keywords as kw
from .aggregate import heat_by_topic, recurring_threat_posters
from .budget import resolve_budget
from .excel_safe import write_sheets
from .filters import _file_label, _find_columns, combine_uploads, screen_dataframe
from .heat import analyze_heat
from .refine import refine
from .storage import persistence_enabled

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


def process(files: list[tuple[str, bytes]], refine_fn=None, heat_fn=None, storage=None) -> Result:
    df = combine_uploads(files)
    text_cols = _find_columns(df, kw.TEXT_COLUMN_OPTIONS)
    screened = screen_dataframe(df)
    screened[_ROW_ID] = range(len(screened))
    storage, storage_failed = _open_storage(storage)
    # Review spends the monthly allowance first; heat uses whatever remains.
    budget = resolve_budget(storage, open_storage=False)
    flagged = screened.loc[screened["category"] != ""].copy()
    flagged = refine(flagged, text_cols, complete=refine_fn, budget=budget)
    screened = analyze_heat(screened, text_cols, complete=heat_fn, budget=budget)
    heat_cols = [c for c in ("heat_score", "heat_topic", "heat_subtopic", "heated_phrases", "heat_sampled")
                 if c in screened.columns]
    if heat_cols and "heat_sampled" in screened.columns and screened["heat_sampled"].fillna(False).astype(bool).any():
        flagged = flagged.drop(columns=[c for c in heat_cols if c in flagged.columns], errors="ignore")
        flagged = flagged.merge(screened[[_ROW_ID, *heat_cols]], on=_ROW_ID, how="left")
    screened = _drop_row_id(screened)
    flagged = _drop_row_id(flagged).reset_index(drop=True)
    res = Result(flagged=flagged, all_posts=screened,
                 posters=recurring_threat_posters(flagged), topics=heat_by_topic(screened))
    note = budget.warning()
    if note:
        res.notes.append(note)
    if storage_failed:
        res.notes.append("Results were not saved to the database.")
    elif storage is not None:
        from .storage import rows_to_store
        try:
            res.run_id = storage.save_run(
                rows_to_store(screened, flagged), [_file_label(n) for n, _ in files], len(df))
        except Exception as exc:  # never lose the user's download because storage failed
            log.error("Saving run failed: %s", type(exc).__name__)
            res.notes.append("Results were not saved to the database.")
    return res


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
