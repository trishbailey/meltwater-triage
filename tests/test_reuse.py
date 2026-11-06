"""Pre-Claude skip for known document ids, URLs, and same-text retweets."""
from __future__ import annotations

import io
import json

import pandas as pd

from triage.pipeline import process, to_workbook
from triage.storage import Storage


def _book(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def _fake(calls):
    def fake(system, user):
        posts = json.loads(user)["posts"]
        kind = "heat" if "heat_score" in system else "refine"
        calls.append((kind, len(posts), [p.get("id") for p in posts]))
        if kind == "heat":
            return json.dumps({"results": [
                {"id": p["id"], "heat_score": 2, "topic": "AUKUS"} for p in posts]})
        return json.dumps({"results": [
            {"id": p["id"], "is_genuine": True, "refined_category": "threat",
             "severity": "medium", "threat_type": "violence", "targets": ["AUKUS program"],
             "narrative_summary": "threat post"}
            for p in posts]})
    return fake


def test_second_upload_skips_claude_for_same_document_ids(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    monkeypatch.setenv("HEAT_MAX_POSTS", "50")
    st = Storage(f"sqlite:///{tmp_path/'r.db'}")
    data = _book([
        {"Document ID": "A1", "Engagement": 10, "Hit Sentence": "kill the AUKUS deal tomorrow"},
        {"Document ID": "A2", "Engagement": 5, "Hit Sentence": "bomb HMAS Stirling next week"},
        {"Document ID": "A3", "Engagement": 1, "Hit Sentence": "nice weather today"},
    ])
    calls = []
    res1 = process([("1.xlsx", data)], refine_fn=_fake(calls), heat_fn=_fake(calls), storage=st)
    assert any(k == "refine" for k, *_ in calls)
    refine_n = sum(n for k, n, *_ in calls if k == "refine")
    assert refine_n == 2  # A1 A2 flagged; the unflagged post never reaches Claude
    assert res1.run_id is not None
    calls.clear()
    res2 = process([("2.xlsx", data)], refine_fn=_fake(calls), heat_fn=_fake(calls), storage=st)
    assert calls == [], calls
    assert len(res2.flagged) == 2
    assert any("Skipped a new review" in n for n in res2.notes)
    # Classification preserved
    assert set(res2.flagged["refined_category"].dropna().astype(str)) == {"threat"}


def test_same_text_retweet_reuses_classification_in_one_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    monkeypatch.setenv("HEAT_MAX_POSTS", "50")
    st = Storage(f"sqlite:///{tmp_path/'t.db'}")
    text = "We will kill the AUKUS deal and bomb the base on Saturday at the harbour"
    data = _book([
        {"Document ID": "R1", "URL": "https://x.com/a/1", "Engagement": 10, "Hit Sentence": text},
        {"Document ID": "R2", "URL": "https://x.com/b/2", "Engagement": 3, "Hit Sentence": text},
        {"Document ID": "R3", "URL": "https://x.com/c/3", "Engagement": 1, "Hit Sentence": "completely different protest rally text about climate"},
    ])
    calls = []
    res = process([("rt.xlsx", data)], refine_fn=_fake(calls), heat_fn=_fake(calls), storage=st)
    refine_calls = [c for c in calls if c[0] == "refine"]
    # Three keyword hits possible; R1/R2 share text so only one refine slot for that text + R3 if flagged
    refine_ids = []
    for c in refine_calls:
        refine_ids.extend(c[2])
    assert len(refine_ids) <= 2  # not 3
    assert len(res.flagged) >= 2


def test_same_file_url_and_text_duplicates_collapse(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    st = Storage(f"sqlite:///{tmp_path/'h.db'}")
    text = "We will kill the AUKUS deal and bomb the base on Saturday at the harbour"
    data = _book([
        {"Document ID": "H1", "URL": "https://x.com/a/1", "Engagement": 10, "Hit Sentence": text},
        {"Document ID": "H2", "URL": "https://x.com/a/1", "Engagement": 4,
         "Hit Sentence": text + " and bring a banner to the gate"},
        {"Document ID": "H3", "URL": "https://x.com/b/9", "Engagement": 3, "Hit Sentence": text},
    ])
    calls = []
    process([("h.xlsx", data)], refine_fn=_fake(calls), storage=st)
    assert sum(n for _, n, *_ in calls) == 1, calls  # one Claude slot for the three copies


def test_fingerprint_helper_ignores_urls_and_case():
    from triage.reuse import text_fingerprint, normalize_text
    a = text_fingerprint("Kill the AUKUS submarine deal at Stirling https://example.com/x now please")
    b = text_fingerprint("kill the aukus submarine deal at stirling www.example.com/y now please")
    assert a and a == b
    assert normalize_text("  Hello\nWorld  ") == "hello world"
