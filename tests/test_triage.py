import base64
import io
import os

import pandas as pd
import pytest

from triage.filters import combine_uploads, flag_posts, screen_text, Patterns, to_excel_bytes

P = Patterns()


def synthetic_df():
    return pd.DataFrame({
        "Date": ["2026-11-01"] * 5,
        "URL": [f"https://example.com/{i}" for i in range(5)],
        "Source Name": ["Twitter"] * 5,
        "Author Name": ["a", "b", "c", "d", "e"],
        "Opening Text": [""] * 5,
        "Hit Sentence": [
            "Someone should blow up the AUKUS submarine base.",          # threat
            "Join us for a rally at Parliament House this Saturday 10am #NoAUKUS",  # protest
            "We will attack HMAS Stirling. Join us, blockade the gates tomorrow",   # both
            "The weather is nice today.",                                   # none
            "Strike action at the bakery.",                                 # excluded union
        ],
    })


def test_threat_requires_target():
    assert screen_text("I will kill time today", P)["category"] == ""
    assert screen_text("kill the AUKUS deal", P)["category"] == "threat"


def test_whole_word_matching():
    assert screen_text("AUKUS skills program", P)["category"] == ""  # 'kill' inside 'skills'


def test_flag_categories_and_columns():
    df = synthetic_df()
    out = flag_posts(df)
    assert list(out["category"]) == ["threat", "protest", "both"]
    for col in df.columns:
        assert col in out.columns
    assert {"matched_keywords", "category", "link"} <= set(out.columns)
    assert out.loc[0, "link"] == "https://example.com/0"
    assert "threat:blow up" in out.loc[0, "matched_keywords"]


def test_upload_roundtrip_xlsx_and_csv():
    df = synthetic_df()
    xbuf = io.BytesIO(); df.to_excel(xbuf, index=False)
    csv = df.to_csv(index=False).encode()
    combined = combine_uploads([("a.xlsx", xbuf.getvalue()), ("b.csv", csv)])
    assert len(combined) == 10
    assert len(flag_posts(combined)) == 6
    assert to_excel_bytes(flag_posts(combined))[:2] == b"PK"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-pw")
    from app import create_app
    return create_app().test_client()


def _auth(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"u:{pw}".encode()).decode()}


def test_auth_required(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth("wrong")).status_code == 401
    assert client.get("/", headers=_auth("test-pw")).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_no_password_configured(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from app import create_app
    assert create_app().test_client().get("/").status_code == 503


def test_upload_endpoint(client):
    xbuf = io.BytesIO(); synthetic_df().to_excel(xbuf, index=False); xbuf.seek(0)
    r = client.post("/upload", headers=_auth("test-pw"),
                    data={"files": (xbuf, "export.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 200
    out = pd.read_excel(io.BytesIO(r.data))
    assert list(out["category"]) == ["threat", "protest", "both"]


SAMPLE = os.environ.get("MELTWATER_SAMPLE_PATH")


@pytest.mark.skipif(not SAMPLE or not os.path.exists(SAMPLE or ""), reason="MELTWATER_SAMPLE_PATH not set")
def test_real_sample():
    with open(SAMPLE, "rb") as fh:
        df = combine_uploads([(os.path.basename(SAMPLE), fh.read())])
    out = flag_posts(df)
    print("\nSample rows:", len(df), "flagged:", out["category"].value_counts().to_dict())
    assert set(out["category"]) <= {"threat", "protest", "both"}


def test_refine_skipped_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = flag_posts(synthetic_df())
    assert "severity" not in out.columns


def test_refine_with_mock_batches():
    import json
    from triage.refine import refine, REFINE_COLUMNS
    calls = []

    def fake(system, user):
        posts = json.loads(user)["posts"]
        calls.append(len(posts))
        return json.dumps({"results": [
            {"id": p["id"], "is_genuine": p["id"] != 1, "refined_category": "THREAT",
             "narrative_summary": "s", "theme": "t", "severity": "High"} for p in posts]})

    flagged = flag_posts(synthetic_df(), refine_fn=lambda s, u: '{"results": []}')
    out = refine(flagged.drop(columns=REFINE_COLUMNS), ["Hit Sentence"], complete=fake, batch_size=2)
    assert calls == [2, 1]
    assert list(out["is_genuine"]) == [True, False, True]
    assert set(out["refined_category"]) == {"threat"} and set(out["severity"]) == {"high"}


def test_refine_failure_keeps_rows():
    def boom(s, u):
        raise RuntimeError("api down")
    out = flag_posts(synthetic_df(), refine_fn=boom)
    assert len(out) == 3 and out["severity"].isna().all()
