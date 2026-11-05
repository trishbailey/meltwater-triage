"""End-to-end processing of one upload: screen, review, heat, aggregate, persist."""
from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field

import pandas as pd

from . import keywords as kw
from .aggregate import heat_by_topic, recurring_threat_posters
from .filters import _find_columns, combine_uploads, screen_dataframe
from .heat import analyze_heat
from .refine import refine


from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

log = logging.getLogger(__name__)

EXCEL_CELL_LIMIT = 32767


@dataclass
class Result:
    flagged: pd.DataFrame
    all_posts: pd.DataFrame
    posters: pd.DataFrame
    topics: pd.DataFrame
    run_id: int | None = None
    notes: list[str] = field(default_factory=list)


def persistence_enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL")) or os.environ.get("PERSIST_RUNS") == "1"


def process(files: list[tuple[str, bytes]], refine_fn=None, heat_fn=None, storage=None) -> Result:
    df = combine_uploads(files)
    text_cols = _find_columns(df, kw.TEXT_COLUMN_OPTIONS)
    screened = screen_dataframe(df)
    screened = analyze_heat(screened, text_cols, complete=heat_fn)
    flagged = screened[screened["category"] != ""].reset_index(drop=True)
    flagged = refine(flagged, text_cols, complete=refine_fn)
    res = Result(flagged=flagged, all_posts=screened,
                 posters=recurring_threat_posters(flagged), topics=heat_by_topic(screened))
    if storage is None and persistence_enabled():
        from .storage import Storage
        storage = Storage()
    if storage is not None:
        from .storage import rows_to_store
        try:
            res.run_id = storage.save_run(rows_to_store(screened, flagged), [n for n, _ in files], len(df))
        except Exception as exc:  # never lose the user's download because storage failed
            log.error("Saving run failed: %s", type(exc).__name__)
            res.notes.append("Results were not saved to the database.")
    return res


def _excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Strip characters Excel rejects and truncate to the cell limit."""
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(lambda v: ILLEGAL_CHARACTERS_RE.sub("", v)[:EXCEL_CELL_LIMIT]
                                    if isinstance(v, str) else v)
    return out


def _neutralize_formulas(book) -> None:
    """Post text such as "=HYPERLINK(...)" must stay text, never run as a formula."""
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"


def to_workbook(res: Result) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        _excel_safe(res.flagged).to_excel(xw, index=False, sheet_name="Flagged")
        _excel_safe(res.posters).to_excel(xw, index=False, sheet_name="Recurring threat posters")
        if "heat_score" in res.all_posts:
            _excel_safe(res.topics).to_excel(xw, index=False, sheet_name="Heat by topic")
            scored = res.all_posts[res.all_posts["heat_sampled"]]
            _excel_safe(scored).to_excel(xw, index=False, sheet_name="Heat (all posts)")
        _neutralize_formulas(xw.book)
    return buf.getvalue()
