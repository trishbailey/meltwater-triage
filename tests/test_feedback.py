
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
