"""End-to-end processing of one upload: screen, review, heat, aggregate, persist."""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field

import pandas as pd

from . import keywords as kw
from .aggregate import heat_by_topic, recurring_threat_posters
from .filters import _find_columns, combine_uploads, screen_dataframe
from .heat import analyze_heat
from .refine import refine


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
        res.run_id = storage.save_run(rows_to_store(screened, flagged), [n for n, _ in files], len(df))
    return res


def to_workbook(res: Result) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        res.flagged.to_excel(xw, index=False, sheet_name="Flagged")
        res.posters.to_excel(xw, index=False, sheet_name="Recurring threat posters")
        if "heat_score" in res.all_posts:
            res.topics.to_excel(xw, index=False, sheet_name="Heat by topic")
            scored = res.all_posts[res.all_posts["heat_sampled"]]
            scored.to_excel(xw, index=False, sheet_name="Heat (all posts)")
    return buf.getvalue()
