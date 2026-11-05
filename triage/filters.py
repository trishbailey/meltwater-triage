"""Threat and protest screening logic (ported from the starter notebook)."""
from __future__ import annotations

import io
import re
from typing import Iterable

import pandas as pd

from . import keywords as kw


def make_pattern(terms: Iterable[str]):
    terms = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    if not terms:
        return None
    alternatives = "|".join(sorted((re.escape(t) for t in terms), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", flags=re.IGNORECASE)


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


def screen_text(text: str, p: Patterns) -> dict:
    """Return threat/protest matches and mobilization status for one post."""
    threat_terms = matched_terms(text, p.threat)
    target_terms = matched_terms(text, p.target)
    is_threat = bool(threat_terms and target_terms)

    m = {name: matched_terms(text, getattr(p, name)) for name in (
        "action", "cta", "disruption", "organizer", "logistics", "hashtag",
        "inquiry", "past", "future", "union", "defense")}
    dates = DATE_PATTERN.findall(text)
    times = TIME_PATTERN.findall(text)
    addresses = ADDRESS_PATTERN.findall(text)

    action_or_cta = bool(m["action"] or m["cta"] or m["disruption"])
    has_place = bool(m["logistics"] or addresses or PLACE_PATTERN.search(text))
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


def screen_dataframe(df: pd.DataFrame, p: Patterns | None = None) -> pd.DataFrame:
    """Annotate every row; original columns are kept untouched."""
    p = p or Patterns()
    text_cols = _find_columns(df, kw.TEXT_COLUMN_OPTIONS)
    if not text_cols:
        raise ValueError("No post-text column found. Available columns: "
                         + ", ".join(map(str, df.columns)))
    text = df[text_cols].astype(object).fillna("").astype(str).agg(" ".join, axis=1).str.strip()
    results = pd.DataFrame([screen_text(t, p) for t in text], index=df.index)
    out = df.copy()
    link_cols = _find_columns(df, kw.LINK_COLUMN_OPTIONS)
    out["link"] = df[link_cols[0]] if link_cols else ""
    for col in results.columns:
        out[col] = results[col]
    return out


def flag_posts(df: pd.DataFrame, p: Patterns | None = None) -> pd.DataFrame:
    screened = screen_dataframe(df, p)
    return screened[screened["category"] != ""].reset_index(drop=True)


def read_upload(filename: str, data: bytes) -> pd.DataFrame:
    """Read one Meltwater export (xlsx/xls/csv), all non-empty sheets."""
    name = filename.lower()
    frames = []
    if name.endswith((".xlsx", ".xls")):
        book = pd.ExcelFile(io.BytesIO(data))
        for sheet in book.sheet_names:
            sdf = pd.read_excel(book, sheet_name=sheet)
            if sdf.empty:
                continue
            sdf["uploaded_file"] = filename
            sdf["worksheet"] = sheet
            frames.append(sdf)
    elif name.endswith(".csv"):
        sdf = _read_csv(data)
        sdf["uploaded_file"] = filename
        sdf["worksheet"] = ""
        frames.append(sdf)
    else:
        raise ValueError(f"Unsupported file type: {filename}")
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _read_csv(data: bytes) -> pd.DataFrame:
    # Meltwater CSV exports are often UTF-16 tab-separated.
    for enc, sep in (("utf-16", "\t"), ("utf-8-sig", None), ("latin-1", None)):
        try:
            return pd.read_csv(io.BytesIO(data), encoding=enc, sep=sep, engine="python")
        except (UnicodeError, pd.errors.ParserError):
            continue
    raise ValueError("Could not parse CSV file")


def combine_uploads(files: list[tuple[str, bytes]]) -> pd.DataFrame:
    frames = [f for f in (read_upload(n, d) for n, d in files) if not f.empty]
    if not frames:
        raise ValueError("No non-empty worksheets found in the uploaded files.")
    return pd.concat(frames, ignore_index=True, sort=False)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="Flagged")
    return buf.getvalue()
