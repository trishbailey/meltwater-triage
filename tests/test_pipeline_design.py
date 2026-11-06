"""Filter-only Claude review, analysis on approved posts only, and label inheritance."""
import json

import pandas as pd

from triage import propagate, refine, stages
from triage.storage import Storage


def _alert(st, n, text, url=None):
    return st.add_alert({"source": "facebook", "url": url or f"https://facebook.com/p/{n}", "author": "g",
                         "text": text, "posted_at": pd.Timestamp("2026-11-03", tz="UTC").to_pydatetime(),
                         "matched_terms": "", "signal_level": "high"})[0]


def test_filter_prompt_asks_only_for_a_verdict():
    for kind in ("threat", "protest", "both"):
        p = refine.system_prompt(kind)
        assert "is_genuine" in p and "filter only" in p
        assert "narrative_summary" not in p and "targets" not in p
    a = refine.system_prompt("protest", "analyze")
    assert "protest_topic" in a and "is_genuine" not in a


def test_alerts_are_filtered_then_only_relevant_are_analyzed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'p.db'}")
    st = Storage(f"sqlite:///{tmp_path/'p.db'}")
    keep = _alert(st, 1, "Rally outside HMAS Stirling on Saturday 14 November, stop AUKUS. Join us at the gate")
    drop = _alert(st, 2, "Join the blockade against AUKUS, everyone must rally and resist the submarine deal")
    seen = []

    def fake(system, user):
        posts = json.loads(user)["posts"]
        seen.append(system)
        if "filter only" in system:
            return json.dumps({"results": [
                {"id": p["id"], "is_genuine": "stirling" in p["text"].lower(),
                 **({"refined_category": "protest"} if "stirling" in p["text"].lower() else {})} for p in posts]})
        return json.dumps({"results": [{"id": p["id"], "protest_topic": "Military bases and facilities",
                                        "targets": ["HMAS Stirling"], "narrative_summary": "Rally at base"}
                                       for p in posts]})
    assert stages.review_alerts(st, complete=fake) >= 1
    reviews = st.load_alert_reviews()
    assert reviews[keep]["is_genuine"] is True and reviews[drop]["is_genuine"] is False
    assert "narrative_summary" not in reviews[keep]  # filter stage does no analysis
    # nothing is analyzed until an analyst approves it
    assert stages.analyze_relevant(st, complete=fake) == 0
    from triage import feedback as fb
    monkeypatch.setattr(fb, "all_labels", lambda: {("alert", str(keep)): "relevant"})
    assert stages.analyze_relevant(st, complete=fake) == 1
    assert st.load_alert_reviews()[keep]["protest_topic"] == "Military bases and facilities"
    assert stages.analyze_relevant(st, complete=fake) == 0  # already analyzed


def test_similar_unlabeled_posts_inherit_the_judgment():
    df = pd.DataFrame([
        {"item_type": "post", "item_id": "1", "Hit Sentence": "Stop AUKUS rally Sunday at Town Hall Perth", "is_genuine": True},
        {"item_type": "post", "item_id": "2", "Hit Sentence": "RT Stop AUKUS rally Sunday at Town Hall Perth http://x.co", "is_genuine": True},
        {"item_type": "alert", "item_id": "9", "Hit Sentence": "Stop AUKUS rally this Sunday at Town Hall Perth", "is_genuine": True},
        {"item_type": "post", "item_id": "3", "Hit Sentence": "Completely different post about submarines and jobs"},
    ])
    out = propagate.inherited(df, {("post", "1"): "not_relevant"})
    assert {(i["item_type"], i["item_id"]) for i in out["not_relevant"]} == {("post", "2"), ("alert", "9")}
    assert out["relevant"] == []
    saved = []
    labels = propagate.apply(df, {("post", "1"): "relevant"}, lambda items, lab: saved.append((len(items), lab)))
    assert saved == [(2, "relevant")] and labels[("post", "2")] == "relevant"
    # conflicting human judgments inside one cluster pass nothing on
    out = propagate.inherited(df, {("post", "1"): "relevant", ("post", "2"): "not_relevant"})
    assert out == {"relevant": [], "not_relevant": []}
