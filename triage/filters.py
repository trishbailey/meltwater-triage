"""Threat and protest screening logic (ported from the starter notebook)."""
from __future__ import annotations

import io
import os
import re
import zipfile
from typing import Iterable

import pandas as pd

from . import keywords as kw


def make_pattern(terms: Iterable[str], case_sensitive: bool = False):
    terms = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    if not terms:
        return None
    alternatives = "|".join(sorted((re.escape(t) for t in terms), key=len, reverse=True))
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", flags=flags)


def matched_terms(text: str, pattern) -> list[str]:
    if pattern is None:
        return []
    return sorted({m.group(0) for m in pattern.finditer(text)}, key=str.lower)


DATE_PATTERN = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))\b",
    flags=re.IGNORECASE,
)
TIME_PATTERN = re.compile(
    r"\b(?:\d{1,2}(?::\d{2})?\s?(?:am|pm)|\d{1,2}:\d{2}|AEDT|AEST|AWST|ACDT|ACST)\b",
    flags=re.IGNORECASE,
)
ADDRESS_PATTERN = re.compile(
    r"\b\d{1,5}\s+[A-Za-z0-9 .'-]+\b(?:Street|St|Road|Rd|Avenue|Ave|Place|Pl|Terrace|Tce)\b",
    re.IGNORECASE,
)
PLACE_PATTERN = re.compile(r"\b(?:outside|at|meet at|assemble at|gather at)\s+[A-Z][^.!?]{2,60}")


class Patterns:
    """Compiled patterns; rebuilt from the keywords module on creation."""

    def __init__(self, k=kw):
        self.threat = make_pattern(k.THREAT_TERMS)
        self.target = make_pattern(k.AUKUS_TARGET_TERMS + k.PERSON_ROLE_TERMS + k.PERSON_NAMES)
        self.target_cs = make_pattern(k.CASE_SENSITIVE_TARGET_TERMS, case_sensitive=True)
        self.weak_threat = {t.lower() for t in k.CONTEXT_REQUIRED_THREAT_TERMS}
        self.proximity = k.THREAT_PROXIMITY_WORDS
        self.weak_protest = {t.lower() for t in k.WEAK_PROTEST_TERMS}
        self.action = make_pattern(k.ACTION_NOUNS)
        self.cta = make_pattern(k.CALL_TO_ACTION)
        self.disruption = make_pattern(k.DISRUPTION_TERMS)
        self.organizer = make_pattern(k.ORGANIZER_TERMS)
        self.logistics = make_pattern(k.LOGISTICS_TERMS)
        self.hashtag = make_pattern(k.HASHTAGS)
        self.inquiry = make_pattern(k.INQUIRY_TERMS)
        self.past = make_pattern(k.PAST_EVENT_TERMS)
        self.future = make_pattern(k.FUTURE_CUES)
        self.union = make_pattern(k.UNION_ACTION_TERMS)
        self.defense = make_pattern(k.AUKUS_DEFENSE_CONTEXT)


# Protest statuses that count as a protest flag (notebook keeps all but
# "No mobilization signal"; we also drop the explicit "Exclude" status).
PROTEST_FLAG_STATUSES = {
    "Mobilization candidate: action/CTA plus date, time, or place",
    "Possible mobilization: missing date/time/place cue",
    "Past event recap: review separately",
    "Advocacy: inquiry/submission campaign",
}


_WORD = re.compile(r"\w+")


def _word_index(text: str, pos: int) -> int:
    return len(_WORD.findall(text[:pos]))


def _threat_terms_in_context(text: str, p: Patterns, target_positions: list[int]) -> list[str]:
    """Threat terms; weak ones only count when a target is within p.proximity words."""
    if p.threat is None or not target_positions:
        return []
    target_words = [_word_index(text, pos) for pos in target_positions]
    found = set()
    for mt in p.threat.finditer(text):
        term = mt.group(0)
        if term.lower() in p.weak_threat:
            w = _word_index(text, mt.start())
            if not any(abs(w - tw) <= p.proximity for tw in target_words):
                continue
        found.add(term)
    return sorted(found, key=str.lower)


STRONG_PROTEST_GROUPS = ("action", "cta", "disruption", "organizer", "hashtag")


def _drop_weak_protest_terms(m: dict, weak: set) -> None:
    """Remove weak protest terms unless another protest signal is present."""
    strong = any(t.lower() not in weak for g in STRONG_PROTEST_GROUPS for t in m[g])
    if strong:
        return
    for g in m:
        m[g] = [t for t in m[g] if t.lower() not in weak]


def _place_match(text: str, weak: set, has_strong_signal: bool) -> bool:
    mt = PLACE_PATTERN.search(text)
    if not mt:
        return False
    first = mt.group(0).split()[0].lower()
    return has_strong_signal or first not in weak


def screen_text(text: str, p: Patterns) -> dict:
    """Return threat/protest matches and mobilization status for one post."""
    target_spans = [(mt.start(), mt.group(0)) for pat in (p.target, p.target_cs) if pat
                    for mt in pat.finditer(text)]
    target_terms = sorted({t for _, t in target_spans}, key=str.lower)
    threat_terms = _threat_terms_in_context(text, p, [pos for pos, _ in target_spans])
    is_threat = bool(threat_terms and target_terms)

    m = {name: matched_terms(text, getattr(p, name)) for name in (
        "action", "cta", "disruption", "organizer", "logistics", "hashtag",
        "inquiry", "past", "future", "union", "defense")}
    _drop_weak_protest_terms(m, p.weak_protest)
    dates = DATE_PATTERN.findall(text)
    times = TIME_PATTERN.findall(text)
    addresses = ADDRESS_PATTERN.findall(text)

    action_or_cta = bool(m["action"] or m["cta"] or m["disruption"])
    has_place = bool(m["logistics"] or addresses or _place_match(text, p.weak_protest, action_or_cta))
    has_dtp = bool(dates or times or has_place)
    past_only = bool(m["past"] and not m["future"])

    if m["union"] and not m["defense"]:
        status = "Exclude: union action without AUKUS/defense context"
    elif m["inquiry"] and not action_or_cta:
        status = "Advocacy: inquiry/submission campaign"
    elif action_or_cta and has_dtp:
        status = ("Past event recap: review separately" if past_only
                  else "Mobilization candidate: action/CTA plus date, time, or place")
    elif action_or_cta or m["organizer"] or m["hashtag"]:
        status = ("Past event recap: review separately" if past_only
                  else "Possible mobilization: missing date/time/place cue")
    else:
        status = "No mobilization signal"
    is_protest = status in PROTEST_FLAG_STATUSES

    protest_terms = sorted(
        set(m["action"] + m["cta"] + m["disruption"] + m["organizer"] + m["hashtag"] + m["inquiry"]),
        key=str.lower,
    )
    matched: list[str] = []
    if is_threat:
        matched += [f"threat:{t}" for t in threat_terms] + [f"target:{t}" for t in target_terms]
    if is_protest:
        matched += [f"protest:{t}" for t in protest_terms]

    category = "both" if is_threat and is_protest else "threat" if is_threat else "protest" if is_protest else ""
    return {
        "matched_keywords": "; ".join(matched),
        "category": category,
        "mobilization_status": status,
    }


def _find_columns(df: pd.DataFrame, options: list[str]) -> list:
    lower = {str(c).strip().lower(): c for c in df.columns}
    return [lower[o] for o in options if o in lower]


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def max_screen_rows() -> int:
    return _env_int("MAX_SCREEN_ROWS", 100_000)


def max_uncompressed_bytes() -> int:
    return _env_int("MAX_UNCOMPRESSED_MB", 200) * 1024 * 1024


def coerce_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """Parse engagement columns as numbers (handles "1,234" and blanks)."""
    out = df.copy()
    for col in _find_columns(df, kw.NUMERIC_COLUMNS):
        cleaned = out[col].astype(str).str.replace(r"[,\s\"]", "", regex=True)
        out[col] = pd.to_numeric(cleaned.replace({"": None, "nan": None, "None": None}), errors="coerce")
    return out


def screen_dataframe(df: pd.DataFrame, p: Patterns | None = None) -> pd.DataFrame:
    """Annotate every row; original columns are kept untouched."""
    p = p or Patterns()
    text_cols = _find_columns(df, kw.TEXT_COLUMN_OPTIONS)
    if not text_cols:
        cols = ", ".join(map(str, df.columns))
        if len(cols) > 300:
            cols = cols[:300] + "…"
        raise ValueError("No post-text column found. Available columns: " + cols)
    text = df[text_cols].astype(object).fillna("").astype(str).agg(" ".join, axis=1).str.strip()
    results = pd.DataFrame([screen_text(t, p) for t in text], index=df.index)
    out = coerce_numeric(df)
    link_cols = _find_columns(df, kw.LINK_COLUMN_OPTIONS)
    out["link"] = df[link_cols[0]] if link_cols else ""
    for col in results.columns:
        out[col] = results[col]
    return out


def flag_posts(df: pd.DataFrame, p: Patterns | None = None, refine_fn=None) -> pd.DataFrame:
    """Keyword-flag posts, then run the optional review stage (see triage.refine)."""
    from . import refine as refine_mod

    screened = screen_dataframe(df, p)
    flagged = screened[screened["category"] != ""].reset_index(drop=True)
    return refine_mod.refine(flagged, _find_columns(df, kw.TEXT_COLUMN_OPTIONS), complete=refine_fn)


def _row_limit_error() -> ValueError:
    return ValueError(
        f"Upload exceeds {max_screen_rows()} rows. Split the export or raise MAX_SCREEN_ROWS."
    )


def _too_large() -> ValueError:
    limit = max_uncompressed_bytes() // (1024 * 1024)
    return ValueError(f"Spreadsheet expands beyond {limit} MB and was not opened.")


def _guard_xlsx(data: bytes) -> None:
    """Reject spreadsheets that expand past MAX_UNCOMPRESSED_MB, including lying zip headers."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Could not read the spreadsheet.") from exc
    limit = max_uncompressed_bytes()
    total = 0
    with archive:
        for info in archive.infolist():
            if info.file_size > limit or total + max(0, info.file_size) > limit:
                raise _too_large()
            got = 0
            try:
                handle = archive.open(info)
            except RuntimeError as exc:  # encrypted or otherwise unreadable
                raise ValueError("Could not read the spreadsheet.") from exc
            with handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > limit or total + got > limit:
                        raise _too_large()
            total += got


def _file_label(filename: str) -> str:
    label = str(filename).replace("\\", "/").split("/")[-1]
    label = "".join(ch for ch in label if ch.isprintable())
    return (label[:120] or "upload")


def read_upload(filename: str, data: bytes, max_rows: int | None = None) -> pd.DataFrame:
    """Read one Meltwater export (xlsx/csv), all non-empty sheets."""
    name = filename.lower()
    label = _file_label(filename)
    budget = max_screen_rows() if max_rows is None else max_rows
    frames = []
    if name.endswith(".xls") and not name.endswith(".xlsx"):
        raise ValueError("Legacy .xls files are not supported. Save the export as .xlsx or .csv.")
    if name.endswith(".xlsx"):
        _guard_xlsx(data)
        book = pd.ExcelFile(io.BytesIO(data))
        for sheet in book.sheet_names:
            sdf = pd.read_excel(book, sheet_name=sheet, nrows=max(budget, 0) + 1, dtype=str)
            if sdf.empty:
                continue
            if len(sdf) > budget:
                raise _row_limit_error()
            budget -= len(sdf)
            sdf["uploaded_file"] = label
            sdf["worksheet"] = sheet
            frames.append(sdf)
    elif name.endswith(".csv"):
        sdf = _read_csv(data, nrows=budget + 1)
        if len(sdf) > budget:
            raise _row_limit_error()
        sdf["uploaded_file"] = label
        sdf["worksheet"] = ""
        frames.append(sdf)
    else:
        raise ValueError(f"Unsupported file type: {label}")
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _read_csv(data: bytes, nrows: int | None = None) -> pd.DataFrame:
    # Meltwater CSV exports are often UTF-16 tab-separated. dtype=str keeps ids intact.
    for enc, sep in (("utf-16", "\t"), ("utf-8-sig", None), ("latin-1", None)):
        try:
            return pd.read_csv(io.BytesIO(data), encoding=enc, sep=sep, engine="python",
                               dtype=str, nrows=nrows)
        except (UnicodeError, pd.errors.ParserError):
            continue
    raise ValueError("Could not parse CSV file")


def combine_uploads(files: list[tuple[str, bytes]]) -> pd.DataFrame:
    limit = max_screen_rows()
    frames = []
    total = 0
    for name, data in files:
        frame = read_upload(name, data, max_rows=limit - total)
        if frame.empty:
            continue
        total += len(frame)
        frames.append(frame)
    if not frames:
        raise ValueError("No non-empty worksheets found in the uploaded files.")
    return pd.concat(frames, ignore_index=True, sort=False)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    from .excel_safe import write_sheets
    return write_sheets([("Flagged", df)])
