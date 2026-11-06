"""Group target names that are the same stated target. Do not invent one."""
from __future__ import annotations

import re

# Exact phrases only, after lowercasing and stripping a leading "the".
_GROUPS = (
    ("AUKUS", {
        "aukus", "aukus program", "ssn-aukus", "ssn aukus", "aukus deal", "aukus pact",
    }),
    ("Australian government", {
        "australian government", "australian labor government", "albanese government",
        "labor government", "labor party", "australian labor party", "australian labor",
    }),
    ("US bases", {
        "us bases", "us base", "u s bases", "u s base", "american bases", "american base",
        "us military bases", "us military base",
    }),
)
_BLANK = {
    "", "unspecified", "unspecified target", "unknown", "unknown target", "none",
    "n/a", "na", "null", "nan", "other", "n/a target",
}


def _key(label: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (label or "").lower()).strip()
    if text.startswith("the "):
        text = text[4:]
    return text


def canonical_target(label: str) -> str | None:
    """Return a display name, or None when the label is not a stated target."""
    key = _key(label)
    if key in _BLANK:
        return None
    for canon, names in _GROUPS:
        if key in names:
            return canon
    raw = re.sub(r"\s+", " ", (label or "").strip())
    if not raw or _key(raw) in _BLANK:
        return None
    return raw


def unique_targets(labels) -> list[str]:
    out = []
    seen = set()
    for label in labels or []:
        canon = canonical_target(str(label))
        if not canon or canon.lower() in seen:
            continue
        seen.add(canon.lower())
        out.append(canon)
    return out


def split_targets(value) -> list[str]:
    if value is None:
        return []
    try:
        import pandas as pd
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    if not isinstance(value, str):
        value = str(value)
    parts = re.split(r"[;|]", value)
    return unique_targets(p.strip() for p in parts if p.strip())


def keyword_targets(matched: str) -> list[str]:
    labels = []
    for piece in str(matched or "").split(";"):
        piece = piece.strip()
        if piece.lower().startswith("target:"):
            labels.append(piece.split(":", 1)[1].strip())
    return unique_targets(labels)
