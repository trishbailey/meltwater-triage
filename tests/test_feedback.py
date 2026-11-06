
import pytest

PW = "pw"
TOKEN = "tok"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", PW)
    monkeypatch.setenv("ALERT_INGEST_TOKEN", TOKEN)
    from app import create_app
    return create_app().test_client()


def test_relevance_same_origin_and_toggle(client):
    r = client.post("/api/relevance", json={"item_type": "alert", "item_id": "1", "label": "relevant", "source": "web"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/relevance", json={"item_type": "post", "item_id": "doc-1", "label": "not_relevant", "source": "meltwater"})
    assert r.status_code == 200 and r.get_json()["label"] == "not_relevant"
    r = client.post("/api/relevance", json={"item_type": "post", "item_id": "doc-1", "label": "relevant", "source": "meltwater"})
    assert r.get_json()["label"] == "relevant"
    client.post("/api/alerts", json={"source": "web", "url": "https://example.com/r1", "signal_level": "low",
                                     "text": "Join the rally outside Parliament House on Saturday 10am to stop AUKUS", "matched_terms": []},
                headers={"Authorization": f"Bearer {TOKEN}"})
    html = client.get("/dashboard").get_data(as_text=True)
    assert "show-hidden" in html and "Relevant" in html and "Not relevant" in html


def test_reasons_saved_and_feed_the_prompt(client, tmp_path, monkeypatch):
    from triage import feedback as fb
    assert "Reshare" not in " ".join(fb.ALL_REASONS.values()) and "reshare" not in " ".join(fb.ALL_REASONS.values()).lower()
    r = client.post("/api/relevance/reason", json={"items": [{"item_type": "post", "item_id": "x"}], "reason": "nope"})
    assert r.status_code == 400
    ids = [f"d{i}" for i in range(3)]
    for i in ids:
        client.post("/api/relevance", json={"item_type": "post", "item_id": i, "label": "not_relevant"})
    items = [{"item_type": "post", "item_id": i} for i in ids]
    r = client.post("/api/relevance/reason", json={"items": items, "reason": "advocacy"})
    assert r.status_code == 200 and r.get_json()["saved"] == 3
    assert fb.reason_counts("not_relevant") == {"advocacy": 3}
    from triage import refine
    block = refine.few_shot_block()
    assert "Standing rules" in block and "advocacy or petition" in block
    client.post("/api/relevance/reason", json={"items": items, "reason": ""})  # clear
    assert fb.reason_counts("not_relevant") == {}
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'id="reason-pop"' in html and "Advocacy or petition" in html


def test_reason_chips_are_in_the_page(client):
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'id="reason-pop"' in html and "/api/relevance/reason" in html and "reason-row" not in html
