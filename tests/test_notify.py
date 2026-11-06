import pandas as pd

from triage import notify, stages
from triage.storage import Storage, rows_to_store


def _store(tmp_path, n=2):
    st = Storage(f"sqlite:///{tmp_path/'n.db'}")
    df = pd.DataFrame([{
        "Document ID": f"d{i}", "Date": "2026-11-03", "URL": f"https://x.com/a/{i}",
        "Hit Sentence": f"Rally at HMAS Stirling on Saturday number {i}, stop AUKUS", "Author Handle": "@a", "Engagement": 1,
        "category": "protest", "is_flagged": True, "is_genuine": True, "refined_category": "protest",
        "post_date": pd.Timestamp("2026-11-03"), "post_month": "2026-11", "origin_source": "X"} for i in range(n)])
    st.save_run(rows_to_store(df, df), ["s.xlsx"], n)
    return st


def test_first_run_seeds_then_alerts_only_on_new_posts(tmp_path, monkeypatch):
    st = _store(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "_post_webhook", lambda url, text, title="": sent.append(text))
    env = {"ALERT_WEBHOOK_URL": "https://hooks.example/x", "RENDER_EXTERNAL_URL": "https://app.example"}
    assert notify.announce(st, env) == {"seeded": 2} and sent == []
    assert notify.announce(st, env) == {"new": 0}
    df = pd.DataFrame([{"Document ID": "d9", "Date": "2026-11-04", "URL": "https://x.com/a/9",
                        "Hit Sentence": "Blockade the Henderson shipyard Friday, bring banners", "Author Handle": "@b",
                        "Engagement": 1, "category": "threat", "is_flagged": True, "is_genuine": True,
                        "refined_category": "threat", "post_date": pd.Timestamp("2026-11-04"),
                        "post_month": "2026-11", "origin_source": "Bluesky"}])
    st.save_run(rows_to_store(df, df), ["t.xlsx"], 1)
    out = notify.announce(st, env)
    assert out == {"new": 1, "sent": ["webhook"]}
    assert "1 threat" in sent[0] and "https://app.example/dashboard" in sent[0]
    assert notify.announce(st, env) == {"new": 0}  # not announced twice


def test_unsent_posts_stay_pending_when_no_channel(tmp_path):
    st = _store(tmp_path, 1)
    assert notify.announce(st, {}) == {"seeded": 1}
    df = pd.DataFrame([{"Document ID": "z", "Date": "2026-11-04", "URL": "https://x.com/a/z",
                        "Hit Sentence": "Rally Saturday at Town Hall Perth against AUKUS", "Author Handle": "@c",
                        "Engagement": 1, "category": "protest", "is_flagged": True, "is_genuine": True,
                        "refined_category": "protest", "post_date": pd.Timestamp("2026-11-04"), "post_month": "2026-11"}])
    st.save_run(rows_to_store(df, df), ["t.xlsx"], 1)
    assert notify.announce(st, {}) == {"new": 1, "sent": []}
    assert notify.announce(st, {})["new"] == 1  # still pending until a channel exists


def test_rejected_and_unreviewed_posts_are_not_ready():
    df = pd.DataFrame([{"is_genuine": False, "refined_category": "none"},
                       {"is_genuine": None, "refined_category": ""},
                       {"is_genuine": True, "refined_category": "threat"}])
    assert len(notify.ready_for_review(df)) == 1


def test_review_loop_needs_a_key_and_runs(tmp_path):
    from triage.collect import start_review_loop
    assert start_review_loop(object(), env={}) is None
    assert start_review_loop(object(), env={"ANTHROPIC_API_KEY": "k", "REVIEW_LOOP": "0"}) is None


def test_run_all_does_not_overlap(tmp_path):
    st = _store(tmp_path, 1)
    assert stages._RUN_LOCK.acquire(blocking=False)
    try:
        assert stages.run_all(st) == {"skipped": "another run is in progress"}
    finally:
        stages._RUN_LOCK.release()


def test_teams_gets_an_adaptive_card_and_slack_gets_text():
    teams = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=x"
    p = notify.payload_for(teams, "line one\n\nhttps://app/dashboard", "AUKUS monitor: 2 protests ready for review")
    assert p["type"] == "message" and p["attachments"][0]["contentType"].endswith("adaptive")
    blocks = p["attachments"][0]["content"]["body"]
    assert blocks[0]["text"].startswith("AUKUS monitor") and len(blocks) == 3
    assert notify.is_teams("https://contoso.webhook.office.com/webhookb2/x")
    assert notify.payload_for("https://hooks.slack.com/services/x", "hi") == {"text": "hi"}
