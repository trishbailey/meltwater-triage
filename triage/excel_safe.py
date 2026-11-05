"""Write dataframes to xlsx without formula cells or illegal characters."""
from __future__ import annotations

import io

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

EXCEL_CELL_LIMIT = 32767


def _clean_text(value: str) -> str:
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:EXCEL_CELL_LIMIT]


def excel_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Copy `df` with worksheet-safe column names and cell text."""
    out = df.copy()
    renamed = []
    for col in out.columns:
        text = _clean_text(str(col)).strip()
        renamed.append(text or "column")
    out.columns = renamed
    for col in out.columns:
        if pd.api.types.is_object_dtype(out[col]) or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(lambda v: _clean_text(v) if isinstance(v, str) else v)
    return out


def neutralize_formulas(book) -> None:
    """Keep text that starts with '=' as text. Excel runs formula cells."""
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"


def write_sheets(sheets: list[tuple[str, pd.DataFrame]]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets:
            excel_safe(df).to_excel(writer, index=False, sheet_name=name[:31])
        neutralize_formulas(writer.book)
    return buf.getvalue()
