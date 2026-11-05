"""Optional second-pass review of flagged posts with the OpenAI API.

Enabled only when OPENAI_API_KEY is set (and REFINE_DISABLED is not "1").
The key is read from the environment and is never logged or printed.
Model: OPENAI_MODEL (default gpt-4o-mini). Batch size: REFINE_BATCH_SIZE (default 20).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"
REFINE_COLUMNS = ["is_genuine", "refined_category", "narrative_summary", "theme", "severity"]
VALID_CATEGORIES = {"threat", "protest", "both", "none"}
VALID_SEVERITY = {"low", "medium", "high", "critical"}

SYSTEM_PROMPT = (
    "You review social media and news posts that a keyword filter flagged as possible threats "
    "(violence, sabotage, intimidation against a defense program, facilities, or people) or "
    "protest mobilization (calls to rally, blockade, or take direct action). For each post, decide "
    "whether the flag is genuine. Return JSON: {\"results\": [{\"id\": int, \"is_genuine\": bool, "
    "\"refined_category\": \"threat\"|\"protest\"|\"both\"|\"none\", \"narrative_summary\": "
    "string (max 30 words), \"theme\": string (2-5 words), \"severity\": "
    "\"low\"|\"medium\"|\"high\"|\"critical\"}]}. One result per input id. Use American spelling."
)

# A completion function takes (system, user) and returns the JSON string reply.
CompleteFn = Callable[[str, str], str]


def is_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY")) and os.environ.get("REFINE_DISABLED") != "1"


def _openai_complete() -> CompleteFn:
    from openai import OpenAI  # imported lazily so the dependency is optional

    client = OpenAI()  # reads OPENAI_API_KEY from the environment
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)

    def complete(system: str, user: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or "{}"

    return complete


def _post_payload(i: int, row: pd.Series, text_cols: list) -> dict:
    text = " ".join(str(row[c]) for c in text_cols if pd.notna(row[c]))
    return {"id": i, "text": text[:2000], "keyword_category": row.get("category", ""),
            "matched_keywords": row.get("matched_keywords", "")}


def _clean(item: dict) -> dict:
    cat = str(item.get("refined_category", "")).lower()
    sev = str(item.get("severity", "")).lower()
    return {
        "is_genuine": bool(item.get("is_genuine")) if "is_genuine" in item else None,
        "refined_category": cat if cat in VALID_CATEGORIES else "",
        "narrative_summary": str(item.get("narrative_summary", ""))[:500],
        "theme": str(item.get("theme", ""))[:100],
        "severity": sev if sev in VALID_SEVERITY else "",
    }


def refine(flagged: pd.DataFrame, text_cols: list, complete: CompleteFn | None = None,
           batch_size: int | None = None) -> pd.DataFrame:
    """Add REFINE_COLUMNS to `flagged`. Returns input unchanged if disabled."""
    if complete is None:
        if not is_enabled():
            return flagged
        complete = _openai_complete()
    if flagged.empty:
        return flagged.assign(**{c: pd.Series(dtype=object) for c in REFINE_COLUMNS})
    batch_size = batch_size or int(os.environ.get("REFINE_BATCH_SIZE", "20"))
    out = flagged.reset_index(drop=True).copy()
    results: dict[int, dict] = {}
    for start in range(0, len(out), batch_size):
        chunk = out.iloc[start:start + batch_size]
        payload = [_post_payload(int(i), r, text_cols) for i, r in chunk.iterrows()]
        try:
            reply = json.loads(complete(SYSTEM_PROMPT, json.dumps({"posts": payload})))
            for item in reply.get("results", []):
                if isinstance(item, dict) and "id" in item:
                    results[int(item["id"])] = _clean(item)
        except Exception as exc:  # keep keyword results if the review fails
            log.warning("Refinement batch at row %d failed: %s", start, type(exc).__name__)
    for col in REFINE_COLUMNS:
        out[col] = [results.get(i, {}).get(col) for i in range(len(out))]
    return out
