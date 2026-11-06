"""Rules that separate a direct threat or a real protest invitation from everything else.

No model call. Advocacy, a news or promotional digest, a policy warning, and
sarcasm or criticism about someone else's action are not the poster's threat
and are not a protest. A high-signal protest needs an invitation, a date, and
a real Australian place.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import geo
from .filters import DATE_PATTERN, Patterns
from .targets import keyword_targets, split_targets

_TRICK = re.compile(r"\bthe old\b.{0,80}\btrick\b", re.I | re.S)
_SO_CAN = re.compile(
    r"\bso\s+(?:that\s+)?(?P<who>(?:@?\w+\s+){1,4}?)can\s+"
    r"(?:bomb|kill|murder|attack|shoot|blow)\b",
    re.I,
)
_THIRD = re.compile(
    r"\b(they|he|she|trump|the president)\s+"
    r"(?:will|can|could|would|did|to)\s+"
    r"(?:bomb|kill|murder|attack|shoot|blow(?:\s+up)?|explode)\b",
    re.I,
)
_FIRST = re.compile(
    r"\b(?:i|we)\s+(?:will|shall|'ll|am going to|are going to)\s+"
    r"(?:bomb|kill|murder|attack|shoot|blow|hang|destroy)\b"
    r"|\bsomeone should\s+(?:bomb|kill|blow|attack|shoot|hang)\b",
    re.I,
)
_INVITE = re.compile(
    r"\b(join us|come along|show up|turn out|all welcome|meet at|assemble at|"
    r"gather at|bring banners|bring placards|rsvp|be there)\b",
    re.I,
)
_ASSEMBLY = re.compile(
    r"\b(rally|protest|march|blockade|picket|demonstration|vigil|sit-in|"
    r"occupation|day of action)\b",
    re.I,
)
_NEWS_PROTEST = re.compile(
    r"\bprotests?\s+in\b|\bprotest(?:s|ers)?\s+(?:erupt|held|reported|over)\b",
    re.I,
)
_REPORT = re.compile(r"\b(reported|according to|coverage of|news that)\b", re.I)
_WARNING = re.compile(r"\b(warns?|warning)\b", re.I)
_RISK = re.compile(r"\bat risk\b|\bputs?\b.{0,40}\bat risk\b", re.I | re.S)
_ADVOCACY = re.compile(
    r"\b(make a submission|submission|petition|have your say|public comment|"
    r"public consultation|inquiry)\b",
    re.I,
)
_STRONG_DIGEST = (
    "roundup", "headlines", "news digest", "watch our latest", "best free tours",
    "weekly news", "this week in the news", "youtube roundup", "latest youtube",
)
_REAL_THREAT_TYPES = {
    "violence", "death threat", "bombing or arson", "sabotage", "doxxing",
    "harassment", "other",
}
# Stated places. Supreme Court Gardens is the park beside the Supreme Court in Perth.
_PLACES = {
    "supreme court gardens": "Supreme Court Gardens",
    "parliament house": "Parliament House",
    "hmas stirling": "HMAS Stirling",
    "garden island": "Garden Island",
    "hyde park": "Hyde Park",
    "federation square": "Federation Square",
    "town hall": "Town Hall",
    "sydney": "Sydney",
    "melbourne": "Melbourne",
    "brisbane": "Brisbane",
    "perth": "Perth",
    "adelaide": "Adelaide",
    "canberra": "Canberra",
    "hobart": "Hobart",
    "darwin": "Darwin",
    "newcastle": "Newcastle",
    "wollongong": "Wollongong",
    "fremantle": "Fremantle",
    "rockingham": "Rockingham",
    "henderson": "Henderson",
    "geelong": "Geelong",
    "townsville": "Townsville",
    "cairns": "Cairns",
    "gold coast": "Gold Coast",
}
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def find_au_place(text: str) -> str:
    """Longest stated place name. 'Canberrans' is not Canberra. The country name is not a place."""
    low = text or ""
    for name in sorted(_PLACES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low, re.I):
            return _PLACES[name]
    return ""


def event_date_text(text: str, today: datetime | None = None) -> str:
    """ISO date when the text states a day and month. No date is invented from a place."""
    match = re.search(
        r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"(?:\s+(\d{4}))?\b",
        text or "", re.I,
    )
    if not match:
        return ""
    day = int(match.group(1))
    month = _MONTHS[match.group(2)[:3].lower()]
    year = int(match.group(3)) if match.group(3) else None
    if year is None:
        now = today or datetime.now(timezone.utc)
        year = now.year
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def is_digest(text: str, author: str = "") -> bool:
    blob = f"{author or ''} {text or ''}".lower()
    compact = re.sub(r"[^a-z0-9]", "", blob)
    if "bestfreetourssydney" in compact:
        return True
    return any(cue in blob for cue in _STRONG_DIGEST)


def is_policy_warning(text: str) -> bool:
    if _INVITE.search(text or ""):
        return False
    return bool(_WARNING.search(text or "") or _RISK.search(text or ""))


def is_attributed_or_sarcasm(text: str) -> bool:
    """Someone else's alleged action, or a joke about it, is not the poster's threat."""
    if _TRICK.search(text or ""):
        return True
    can = _SO_CAN.search(text or "")
    if can and not re.search(r"\b(i|we)\b", can.group("who"), re.I):
        return True
    if _THIRD.search(text or "") and not _FIRST.search(text or ""):
        return True
    return False


def _author(row) -> str:
    from .textnorm import _cell, _text
    parts = []
    for name in ("author handle", "author name", "author"):
        val = _text(_cell(row, name))
        if val:
            parts.append(val)
    return " ".join(parts)


def adjust(text: str, raw: dict, author: str = "") -> dict:
    """Apply the rules to one keyword result. Does not call a model."""
    out = dict(raw)
    reason = []
    place = find_au_place(text)
    has_date = bool(DATE_PATTERN.search(text or ""))
    invite = bool(_INVITE.search(text or ""))
    assembly = bool(_ASSEMBLY.search(text or "")) and not _NEWS_PROTEST.search(text or "")
    warning = is_policy_warning(text)
    digest = is_digest(text, author)
    attributed = is_attributed_or_sarcasm(text)
    advocacy = out.get("mobilization_status", "").startswith("Advocacy") or (
        bool(_ADVOCACY.search(text or "")) and not invite
    )
    high = bool(
        (invite or (assembly and not _REPORT.search(text or "")))
        and has_date and place and not digest and not (warning and not invite)
    )
    suppress_threat = False
    suppress_protest = False
    if attributed:
        suppress_threat = True
        reason.append("third_party_or_sarcasm")
    if digest and not high and not _FIRST.search(text or ""):
        suppress_threat = True
        suppress_protest = True
        reason.append("news_or_promotion")
    if warning and not invite and not high:
        suppress_protest = True
        reason.append("policy_warning")
    if advocacy and not high:
        suppress_protest = True
        reason.append("advocacy_not_a_protest")
    if str(out.get("mobilization_status") or "").startswith("Past event") and not high:
        suppress_protest = True
        reason.append("past_recap")

    cat = out.get("category") or ""
    threat = cat in ("threat", "both") and not suppress_threat
    protest = (cat in ("protest", "both") or high) and not (suppress_protest and not high)
    if high:
        protest = True
    if suppress_threat:
        threat = False
    if threat and protest:
        cat = "both"
    elif threat:
        cat = "threat"
    elif protest:
        cat = "protest"
    else:
        cat = ""
    out["category"] = cat
    out["suppress_threat"] = suppress_threat
    out["suppress_protest"] = suppress_protest and not high
    out["high_signal_protest"] = bool(high and protest)
    out["judgment_reason"] = ",".join(reason)
    if protest and place:
        out["protest_location"] = place
    else:
        out.setdefault("protest_location", "")
    if protest and has_date:
        out["event_date"] = event_date_text(text) or out.get("event_date") or ""
    else:
        out.setdefault("event_date", "")
    return out


def _stored_bool_false(value) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() in {"false", "0", "no"})


_PATTERNS: Patterns | None = None


def _patterns() -> Patterns:
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = Patterns()
    return _PATTERNS


def display_flags(row, patterns: Patterns | None = None) -> dict:
    """How a stored row should count. Rules override a stored label they reject.

    A stored threat type is kept when the text has no reject-reason and the type
    is a real direct-threat type. 'call to protest' and 'none' are not direct threats.
    Advocacy is never turned into a threat.
    """
    from .filters import screen_text
    from .textnorm import single_post_text

    text = single_post_text(row)
    author = _author(row)
    if text:
        raw = screen_text(text, patterns or _patterns(), author=author)
    else:
        raw = adjust("", {"category": "", "mobilization_status": "", "matched_keywords": ""}, author)
    status = str(_cell_status(row))
    tt = str(_cell(row, "threat_type") or "").strip().lower()
    cat = str(_cell(row, "category") or "").strip().lower()
    refined = str(_cell(row, "refined_category") or "").strip().lower()
    reason = raw.get("judgment_reason") or ""
    threat = raw["category"] in ("threat", "both")
    protest = raw["category"] in ("protest", "both") or bool(raw.get("high_signal_protest"))
    if not reason and not threat and not protest and not _stored_bool_false(_cell(row, "is_genuine")):
        stored_threat = cat in ("threat", "both") or refined in ("threat", "both")
        stored_protest = cat in ("protest", "both") or refined in ("protest", "both")
        if stored_threat and tt in _REAL_THREAT_TYPES:
            threat = True
        if stored_protest and "advoc" not in status.lower():
            protest = True
    if "advoc" in status.lower() and not raw.get("high_signal_protest"):
        protest = False
        if raw["category"] not in ("threat", "both"):
            threat = False
    if raw.get("suppress_threat"):
        threat = False
    if raw.get("suppress_protest") and not raw.get("high_signal_protest"):
        protest = False
    if tt == "call to protest" and raw["category"] not in ("threat", "both"):
        threat = False
    if raw.get("high_signal_protest"):
        protest = True
    if raw["category"] in ("threat", "both") and not raw.get("suppress_threat"):
        threat = True
    return {"threat": threat, "protest": protest, "raw": raw, "text": text, "author": author}


def _cell(row, name: str):
    from .textnorm import _cell as cell
    return cell(row, name)


def _cell_status(row) -> str:
    val = _cell(row, "mobilization_status")
    return "" if val is None else str(val)


def stated_targets(row, raw: dict | None = None) -> list[str]:
    """Stored target names, or keyword targets when the stored field is blank.

    A blank field is not labeled as a target. Keyword hits are terms already
    matched in the text, not a guessed institution.
    """
    stored = split_targets(_cell(row, "targets"))
    if stored:
        return stored
    matched = ""
    if raw:
        matched = raw.get("matched_keywords") or ""
    if not matched:
        matched = _cell(row, "matched_keywords") or ""
    return keyword_targets(str(matched or ""))


def apply_judgment_df(df, drop_cleared: bool = True):
    """Set category from the rules. Optionally drop rows that are not threat or protest."""
    import pandas as pd
    if df is None or df.empty:
        return df
    patterns = Patterns()
    out = df.copy()
    keep = []
    for i, row in out.iterrows():
        flags = display_flags(row.to_dict(), patterns)
        threat, protest = flags["threat"], flags["protest"]
        raw = flags["raw"]
        if threat and protest:
            cat = "both"
        elif threat:
            cat = "threat"
        elif protest:
            cat = "protest"
        else:
            cat = ""
        out.at[i, "category"] = cat
        if protest and raw.get("protest_location") and not str(row.get("protest_location") or "").strip():
            out.at[i, "protest_location"] = raw.get("protest_location") or ""
        if protest and raw.get("event_date") and not str(row.get("event_date") or "").strip():
            out.at[i, "event_date"] = raw.get("event_date") or ""
        if cat == "" and (raw.get("suppress_threat") or raw.get("suppress_protest") or raw.get("judgment_reason")):
            out.at[i, "is_genuine"] = False
            if "refined_category" in out.columns or True:
                out.at[i, "refined_category"] = "none"
            tt = str(row.get("threat_type") or "").strip().lower()
            if tt in {"", "none", "call to protest", "unspecified", "unknown"}:
                out.at[i, "threat_type"] = "none"
            out.at[i, "judgment_reason"] = raw.get("judgment_reason") or ""
        elif "refined_category" in out.columns:
            refined = str(row.get("refined_category") or "").strip().lower()
            if refined in {"threat", "both", "protest", "none"} and refined != cat and cat:
                # Keep a model label only when it still agrees with the rules.
                if (refined in {"threat", "both"} and not threat) or (refined == "protest" and not protest):
                    out.at[i, "refined_category"] = cat
        keep.append(bool(cat))
    if drop_cleared:
        out = out.loc[keep].copy()
    return out


def place_in_australia(name: str) -> bool:
    hit = geo.geocode(name) if name else None
    if not hit:
        return False
    return geo.in_australia(hit["lat"], hit["lon"])
