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


def _row(i, text, cat="threat", date="2026-11-04"):
    return {"Document ID": i, "Date": date, "URL": f"https://x.com/a/{i}", "Hit Sentence": text,
            "Author Handle": "@b", "Engagement": 1, "category": cat, "is_flagged": True, "is_genuine": True,
            "refined_category": cat, "post_date": pd.Timestamp(date), "post_month": date[:7], "origin_source": "X"}


def _add(st, rows):
    df = pd.DataFrame(rows)
    st.save_run(rows_to_store(df, df), ["t.xlsx"], len(df))


ENV = {"ALERT_WEBHOOK_URL": "https://hooks.example/x", "RENDER_EXTERNAL_URL": "https://app.example"}


def test_ordinary_new_posts_send_nothing(tmp_path, monkeypatch):
    st = _store(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "_post_webhook", lambda url, text, title="": sent.append(text))
    assert notify.announce(st, ENV) == {"new": 0}
    _add(st, [_row("n1", "Rally Saturday at Town Hall Perth against AUKUS", "protest")])
    assert notify.announce(st, ENV) == {"new": 0} and sent == []


def test_clear_threat_alerts_once(tmp_path, monkeypatch):
    st = _store(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "_post_webhook", lambda url, text, title="": sent.append(text))
    notify.announce(st, ENV)
    _add(st, [_row("t1", "I will blow up HMAS Stirling this weekend")])
    out = notify.announce(st, ENV)
    assert out["threats"] == 1 and out["sent"] == ["webhook"]
    assert "clear threat" in sent[0] and "https://app.example/dashboard" in sent[0]
    assert notify.announce(st, ENV) == {"new": 0}
    assert len(sent) == 1


def test_unsent_threat_stays_pending_without_a_channel(tmp_path):
    st = _store(tmp_path)
    notify.announce(st, {})
    _add(st, [_row("t2", "I will blow up HMAS Stirling this weekend")])
    assert notify.announce(st, {}) == {"threats": 1, "spikes": [], "sent": []}
    assert notify.announce(st, {})["threats"] == 1


def test_ambiguous_threat_language_does_not_alert():
    for t in ("Kill the AUKUS deal now", "Defence minister said they would kill the program",
              "Russia could attack the submarine base", "Shoot the messenger, said the senator"):
        assert not notify.is_clear_threat({"category": "threat", "Hit Sentence": t}), t
    assert notify.is_clear_threat({"category": "threat", "Hit Sentence": "Someone should shoot the defence minister"})


def test_spike_needs_ratio_and_floor():
    now = pd.Timestamp("2026-11-06 12:00")

    def frame(recent, prior):
        rows = [{"category": "protest", "post_date": now - pd.Timedelta(hours=1)}] * recent
        rows += [{"category": "protest", "post_date": now - pd.Timedelta(days=2)}] * prior
        return pd.DataFrame(rows)
    env = {}
    assert [s["category"] for s in notify.spikes(frame(40, 56), env, now)] == ["protest"]
    assert notify.spikes(frame(20, 0), env, now) == []        # below the 30-post floor
    assert notify.spikes(frame(40, 560), env, now) == []      # busy week: 40 is under 3x the average
    assert notify.spikes(frame(40, 56), {"ALERT_SPIKE_MIN_POSTS": "100"}, now) == []


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
