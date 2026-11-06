"""AI review of the posts the pre-sort flagged as threats or protest mobilization.

Claude never sees unflagged posts. The code pre-sorts first (triage.filters), so this stage
handles only the small set that survived. Each post is reviewed with a prompt that asks for
the fields its category needs (threat, protest, or both) and nothing else, which keeps the
reply short and the call fast.

Enabled only when ANTHROPIC_API_KEY is set (and REFINE_DISABLED is not "1"). The key is read
from the environment and is never logged or printed.
Settings: REFINE_MODEL (default claude-haiku-4-5), REFINE_BATCH_SIZE (default 25),
ANTHROPIC_CONCURRENCY (default 12, max 16), ANTHROPIC_MAX_TOKENS (default 4096),
ANTHROPIC_TIMEOUT_SECONDS (default 120). ANTHROPIC_MAX_RETRIES defaults to 0 so one batch is
one request. ANTHROPIC_MONTHLY_POST_CAP (see triage.budget) can skip this stage.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import pandas as pd

from .taxonomy import PROTEST_TOPICS, canon_topic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
REFINE_COLUMNS = [
    "is_genuine", "refined_category", "narrative_summary", "theme", "severity",
    "threat_type", "targets", "named_individuals",
    "protest_location", "protest_topic", "organizers_or_groups", "event_date", "scale_or_turnout",
]
THREAT_TYPES = {
    "violence", "death threat", "bombing or arson", "sabotage", "doxxing", "harassment",
    "call to protest", "call to disruption", "other", "none",
}
VALID_CATEGORIES = {"threat", "protest", "both", "none"}
VALID_SEVERITY = {"low", "medium", "high", "critical"}

_CONTEXT = (
    "You review social media and news posts about the AUKUS defense program in Australia. A "
    "keyword pre-sort flagged each post below as a possible {what}. Decide whether the flag is "
    "genuine. This is a filter only: do no analysis. Return JSON {{\"results\": [{{...}}]}} with one "
    "result per input id."
)
_ANALYSIS_CONTEXT = (
    "You analyze social media and news posts about the AUKUS defense program in Australia. An "
    "analyst confirmed each post below as a genuine {what}. Extract the fields. Return JSON "
    "{{\"results\": [{{...}}]}} with one result per input id. Use American spelling. Leave out any "
    "field that has no value, so replies stay short."
)
_THREAT_GENUINE = ("\"is_genuine\": bool (true only for violence, sabotage, intimidation, or "
                   "harassment aimed at the program, its facilities, or people)")
_PROTEST_GENUINE = ("\"is_genuine\": bool (true only when the post announces or calls for a specific "
                    "rally, march, blockade, or direct action, or reports one from the last week, and gives at "
                    "least one of a date, a place, or an organizer. false for petitions, submissions to "
                    "inquiries, petitions or any call to sign or add your name, opinion, news commentary, general opposition to AUKUS, reshares with no new "
                    "detail, and recaps of events older than a week)")
_THREAT_FILTER = "\"id\": int, " + _THREAT_GENUINE
_PROTEST_FILTER = "\"id\": int, " + _PROTEST_GENUINE
_BOTH_FILTER = ("\"id\": int, \"is_genuine\": bool (true when the post is a genuine threat as defined "
                "below or a genuine protest call as defined below), \"refined_category\": \"threat\"|"
                "\"protest\"|\"both\"|\"none\". Threat: violence, sabotage, intimidation, or harassment "
                "aimed at the program, its facilities, or people. Protest: announces or calls for a specific "
                "rally, march, blockade, or direct action, or reports one from the last week, with a date, "
                "place, or organizer; petitions, opinion, commentary, and reshares are not protests")
_THREAT_FIELDS = (
    "\"id\": int, \"severity\": "
    "\"low\"|\"medium\"|\"high\"|\"critical\", \"threat_type\": one of \"violence\"|"
    "\"death threat\"|\"bombing or arson\"|\"sabotage\"|\"doxxing\"|\"harassment\"|"
    "\"call to disruption\"|\"other\", \"targets\": [specific physical locations or assets the post "
    "threatens, e.g. HMAS Stirling, Henderson shipyard, Pine Gap, a named vessel or office; never a general subject such as AUKUS, "
    "Australia, the government or the Navy; leave empty when none is named], \"named_individuals\": [named politicians and other named people], "
    "\"theme\": string (2-5 words), \"narrative_summary\": string (max 20 words)"
)
_PROTEST_FIELDS = (
    "\"id\": int, \"protest_topic\": exactly one of \"" + "\"|\"".join(PROTEST_TOPICS) + "\", "
    "\"protest_location\": string (city, region, or site), \"organizers_or_groups\": [who is behind it], "
    "\"targets\": [specific physical places the protest is held at or aimed at, one per entry, e.g. HMAS Stirling, "
    "Henderson shipyard, Pine Gap, Parliament House Canberra, US Consulate Sydney, Sydney Town Hall; never a "
    "general subject such as AUKUS, the government, or the Navy; leave empty when no physical place is named], \"event_date\": string (only if stated), "
    "\"scale_or_turnout\": string (only if stated), \"narrative_summary\": string (max 20 words)"
)
_BOTH_FIELDS = (
    "\"id\": int, plus every threat field (severity, threat_type, targets, named_individuals, "
    "theme) and every protest field (protest_topic, protest_location, organizers_or_groups, "
    "event_date, scale_or_turnout) that applies, and \"narrative_summary\": string (max 20 words)"
)
PROMPT_KINDS = {
    "threat": ("threat (violence, sabotage, or intimidation)", _THREAT_FILTER, _THREAT_FIELDS),
    "protest": ("protest or mobilization (a call to rally, blockade, or direct action)", _PROTEST_FILTER,
                _PROTEST_FIELDS),
    "both": ("threat and protest mobilization", _BOTH_FILTER, _BOTH_FIELDS),
}


def system_prompt(kind: str, mode: str = "filter") -> str:
    """mode "filter": keep or discard only. mode "analyze": topic, targets and the rest, for
    posts an analyst already marked relevant."""
    what, filter_fields, analysis_fields = PROMPT_KINDS[kind]
    if mode == "analyze":
        return _ANALYSIS_CONTEXT.format(what=what) + " Each result has: {" + analysis_fields + "}."
    return _CONTEXT.format(what=what) + " Each result has: {" + filter_fields + "}."


SYSTEM_PROMPT = system_prompt("both")

# A completion function takes (system, user) and returns the JSON string reply.
CompleteFn = Callable[[str, str], str]


def is_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("REFINE_DISABLED") != "1"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return min(hi, max(lo, value))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if value > 0 else default


def anthropic_complete(max_tokens: int | None = None) -> CompleteFn:
    import anthropic  # imported lazily so the dependency is optional

    # Reads ANTHROPIC_API_KEY from the environment. Retries default to 0 so the
    # monthly post cap counts each request once.
    client = anthropic.Anthropic(
        timeout=_env_float("ANTHROPIC_TIMEOUT_SECONDS", 120.0),
        max_retries=_env_int("ANTHROPIC_MAX_RETRIES", 0, 0, 5),
    )
    model = os.environ.get("REFINE_MODEL", DEFAULT_MODEL)
    limit = max_tokens if max_tokens is not None else _env_int("ANTHROPIC_MAX_TOKENS", 4096, 256, 64000)

    def complete(system: str, user: str) -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=limit,
            system=system + " Reply with the JSON object only, no other text.",
            messages=[{"role": "user", "content": user}],
        )
        if getattr(resp, "stop_reason", None) == "max_tokens":
            log.warning("Claude response hit the output token limit")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        start, end = text.find("{"), text.rfind("}")
        return text[start:end + 1] if start != -1 else "{}"

    return complete


def concurrency() -> int:
    """Parallel Claude requests (ANTHROPIC_CONCURRENCY, default 12, max 16)."""
    return _env_int("ANTHROPIC_CONCURRENCY", 12, 1, 16)


def run_concurrently(fn, jobs: list) -> list:
    """Run fn over jobs with a small thread pool; results keep job order."""
    if len(jobs) <= 1 or concurrency() == 1:
        return [fn(j) for j in jobs]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(concurrency(), len(jobs))) as pool:
        return list(pool.map(fn, jobs))


def _post_payload(i: int, row: dict, text_cols: list) -> dict:
    text = " ".join(str(row[c]) for c in text_cols if c in row and pd.notna(row[c]))
    return {"id": i, "text": text[:1000],
            "author": " ".join(str(row[c]) for c in row
                               if str(c).lower() in ("author name", "author handle") and pd.notna(row[c])),
            "keyword_category": row.get("category", ""),
            "matched_keywords": row.get("matched_keywords", "")}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "yes", "1"}:
            return True
        if text in {"false", "no", "0"}:
            return False
    return None


def _clean(item: dict, kind: str | None = None) -> dict:
    cat = str(item.get("refined_category", "")).lower()
    genuine = _as_bool(item.get("is_genuine")) if "is_genuine" in item else None
    if cat not in VALID_CATEGORIES:
        # The prompt for a single category does not ask for it; derive it from the verdict.
        cat = ("none" if genuine is False else kind) if kind in ("threat", "protest") else ""
    sev = str(item.get("severity", "")).lower()
    tt = str(item.get("threat_type", "")).strip().lower()
    topic = _text(item.get("protest_topic"), 100)
    topic = topic if topic in PROTEST_TOPICS else canon_topic(topic)
    theme = str(item.get("theme", ""))[:100] or topic
    return {
        "is_genuine": genuine,
        "refined_category": cat,
        "narrative_summary": str(item.get("narrative_summary", ""))[:500],
        "theme": theme,
        "severity": sev if sev in VALID_SEVERITY else "",
        "threat_type": tt if tt in THREAT_TYPES else ("other" if tt else ""),
        "targets": _join_list(item.get("targets")),
        "named_individuals": _join_list(item.get("named_individuals")),
        "protest_location": _text(item.get("protest_location"), 200),
        "protest_topic": topic,
        "organizers_or_groups": _join_list(item.get("organizers_or_groups")),
        "event_date": _text(item.get("event_date"), 100),
        "scale_or_turnout": _text(item.get("scale_or_turnout"), 100),
    }


def _text(value, limit: int) -> str:
    return "" if value is None else str(value).strip()[:limit]


def _join_list(value) -> str:
    """Store lists as "; "-joined text (split on "; " for dashboard aggregation)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ""
    seen = []
    for v in value:
        v = str(v).strip()
        if v and v.lower() not in {s.lower() for s in seen}:
            seen.append(v)
    return "; ".join(seen)[:500]


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _absorb(results: dict, reply_text: str, clean) -> None:
    reply = json.loads(reply_text)
    if not isinstance(reply, dict):
        return
    for item in reply.get("results", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            results[int(item["id"])] = clean(item)
        except (TypeError, ValueError, OverflowError):
            continue


def few_shot_block(storage=None) -> str:
    """Analyst-labeled examples for the prompt, one per cluster of similar posts. Built once per run."""
    try:
        from . import feedback as fb
        from .grouping import group_indices
        from .storage import Storage, persistence_enabled
        if not persistence_enabled():
            return ""
        store = storage or Storage()
        labeled = {lab: fb.recent_examples(lab, limit=120) for lab in ("relevant", "not_relevant")}
        why = fb.all_reasons()
        post_ids = [str(r["item_id"]) for rows in labeled.values() for r in rows if r["item_type"] == "post"]
        by_id = store.posts_for_ids(post_ids) if post_ids else {}
        alert_text = {}
        if any(r["item_type"] == "alert" for rows in labeled.values() for r in rows):
            adf = store.load_alerts(limit=500)
            alert_text = {str(r["id"]): str(r.get("text") or "") for r in adf.to_dict(orient="records")}
        lines = []
        for lab, title in (("relevant", "RELEVANT (keep)"), ("not_relevant", "NOT RELEVANT (discard)")):
            texts = []
            for row in labeled[lab]:
                rec = why.get((row["item_type"], str(row["item_id"]))) or {}
                if row["item_type"] == "alert":
                    t = alert_text.get(str(row["item_id"]), "")
                else:
                    data = by_id.get(str(row["item_id"])) or {}
                    t = str(data.get("Hit Sentence") or data.get("narrative_summary") or "")
                t = t.replace("\n", " ").strip()
                if t:
                    family = fb.NOT_RELEVANT_REASONS if lab == "not_relevant" else fb.RELEVANT_REASONS
                    tag = family.get(rec.get("reason", ""), "")
                    extra = (" [" + tag + (": " + rec["note"] if rec.get("note") else "") + "]") if tag else ""
                    texts.append(t[:240] + extra)
            keep = [g[0] for g in group_indices(texts)][:30]  # one example per pattern
            if keep:
                lines += [title + ":"] + ["- " + texts[i] for i in keep]
        rules = []
        for lab, word in (("not_relevant", "Discard"), ("relevant", "Note")):
            for code, n in sorted(fb.reason_counts(lab).items(), key=lambda kv: -kv[1]):
                if n >= 3 and code != "other":
                    name = fb.ALL_REASONS.get(code, code).lower()
                    rules.append(f"- {word} posts that are: {name} (the analyst said so {n} times)")
        if rules:
            lines = ["Standing rules from the analyst:"] + rules + lines
        return "\n".join(lines)[:8000]
    except Exception:
        return ""


def _kind_of(category) -> str:
    cat = str(category or "")
    return cat if cat in ("threat", "protest", "both") else "both"


def refine(flagged: pd.DataFrame, text_cols: list, complete: CompleteFn | None = None,
           batch_size: int | None = None, budget=None, progress=None,
           examples: str | None = None, mode: str = "filter") -> pd.DataFrame:
    """Add REFINE_COLUMNS to `flagged`. Returns input unchanged if disabled.

    Posts are grouped by category so each batch gets the prompt, and the reply size, for
    that category only.
    """
    if complete is None:
        if not is_enabled():
            return flagged
        complete = anthropic_complete()
    if budget is None:
        from .budget import resolve_budget
        budget = resolve_budget()
    if flagged.empty:
        return flagged.assign(**{c: pd.Series(dtype=object) for c in REFINE_COLUMNS})
    batch_size = _positive_int(
        batch_size if batch_size is not None else os.environ.get("REFINE_BATCH_SIZE", "25"), 25)
    out = flagged.reset_index(drop=True).copy()
    records = out.to_dict(orient="records")
    results: dict[int, dict] = {}
    tip = few_shot_block() if examples is None else examples
    from .rules import prompt_text
    standing = prompt_text()
    if standing:
        tip = "Standing rules set by the analyst:\n" + standing + (("\n\n" + tip) if tip else "")
    by_kind: dict[str, list[int]] = {}
    for i, rec in enumerate(records):
        by_kind.setdefault(_kind_of(rec.get("category")), []).append(i)
    jobs = []
    for kind, ids in by_kind.items():
        system = system_prompt(kind, mode) + (
            ("\n\nThe analyst's own judgments follow. Keep posts like the RELEVANT examples and discard posts "
             "like the NOT RELEVANT examples:\n" + tip) if tip and mode == "filter" else "")
        for start in range(0, len(ids), batch_size):
            chunk_ids = ids[start:start + batch_size]
            allowed = budget.take(len(chunk_ids))  # reserved serially so the caps hold
            if allowed <= 0:
                break
            chunk_ids = chunk_ids[:allowed]
            payload = [_post_payload(i, records[i], text_cols) for i in chunk_ids]
            jobs.append((kind, start, len(chunk_ids), system, json.dumps({"posts": payload})))

    def _run(job):
        kind, start, n, system, body = job
        parsed: dict[int, dict] = {}
        done = None
        try:
            reply = complete(system, body)
            _absorb(parsed, reply, lambda item: _clean(item, kind))
            done = []
            for i, cleaned in parsed.items():
                if not 0 <= i < len(records):
                    continue
                rec = dict(records[i])
                rec.update({k: v for k, v in cleaned.items() if v is not None})
                done.append(rec)
        except Exception as exc:  # keep keyword results if the review call fails
            log.warning("Review batch (%s, row %d) failed: %s", kind, start, type(exc).__name__)
            done = None
            parsed = {}
        # Durably record a completed batch before telling the job it made progress.
        if done and progress:
            progress(n, done)
        elif progress:
            progress(n)
        return parsed
    for parsed in run_concurrently(_run, jobs):
        if parsed:
            results.update(parsed)
    for col in REFINE_COLUMNS:
        out[col] = [results.get(i, {}).get(col) for i in range(len(out))]
    return out
