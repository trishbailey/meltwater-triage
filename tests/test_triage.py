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
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "test-pw")
    from app import create_app, _failures
    _failures.clear()
    c = create_app().test_client()
    assert c.post("/login", data={"password": "test-pw"}).status_code == 302
    return c


def _auth(pw):
    return {}






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
             "narrative_summary": "s", "theme": "t", "severity": "High",
             "threat_type": "Bombing or Arson", "targets": ["HMAS Stirling", "hmas stirling", "Navy"],
             "named_individuals": ["Jane Doe"]} for p in posts]})

    flagged = flag_posts(synthetic_df(), refine_fn=lambda s, u: '{"results": []}')
    out = refine(flagged.drop(columns=REFINE_COLUMNS), ["Hit Sentence"], complete=fake, batch_size=2)
    assert calls == [2, 1]
    assert list(out["is_genuine"]) == [True, False, True]
    assert set(out["refined_category"]) == {"threat"} and set(out["severity"]) == {"high"}
    assert set(out["threat_type"]) == {"bombing or arson"}
    assert out.loc[0, "targets"] == "HMAS Stirling; Navy"
    assert out.loc[0, "named_individuals"] == "Jane Doe"


def test_refine_failure_keeps_rows():
    def boom(s, u):
        raise RuntimeError("api down")
    out = flag_posts(synthetic_df(), refine_fn=boom)
    assert len(out) == 3 and out["severity"].isna().all()


def test_refine_keeps_good_rows_and_parses_false_string(monkeypatch):
    import json
    from triage.refine import refine
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    flagged = flag_posts(synthetic_df()).head(1)

    def fake(system, user):
        return json.dumps({"results": [
            {"id": "bad", "is_genuine": True, "refined_category": "threat"},
            {"id": 0, "is_genuine": "false", "refined_category": "threat", "severity": "low"},
        ]})

    out = refine(flagged.drop(columns=[c for c in flagged.columns if c == "severity"], errors="ignore"),
                 ["Hit Sentence"], complete=fake)
    assert out.loc[0, "is_genuine"] == False
    assert out.loc[0, "severity"] == "low"


def test_upload_rejects_cross_site_origin(client):
    r = client.post("/upload", headers={**_auth("test-pw"), "Origin": "https://evil.example"},
                    data={}, content_type="multipart/form-data")
    assert r.status_code == 403
    r = client.post("/upload", headers={**_auth("test-pw"), "Origin": "null"},
                    data={}, content_type="multipart/form-data")
    assert r.status_code == 403
    xbuf = io.BytesIO()
    synthetic_df().to_excel(xbuf, index=False)
    xbuf.seek(0)
    ok = client.post("/upload", headers={**_auth("test-pw"), "Origin": "http://localhost"},
                     data={"files": (xbuf, "export.xlsx")}, content_type="multipart/form-data")
    assert ok.status_code == 200


def test_row_limit_rejects_before_screening(monkeypatch):
    monkeypatch.setenv("MAX_SCREEN_ROWS", "2")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    csv = b"Hit Sentence\nkill the AUKUS deal\nbomb HMAS Stirling\nattack the prime minister\n"
    with pytest.raises(ValueError, match="exceeds 2 rows"):
        combine_uploads([("a.csv", csv)])


def test_legacy_xls_is_rejected():
    with pytest.raises(ValueError, match=".xls"):
        combine_uploads([("old.xls", b"not a workbook")])


def test_xlsx_expansion_is_rejected(monkeypatch):
    import zipfile
    monkeypatch.setenv("MAX_UNCOMPRESSED_MB", "1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"x" * (2 * 1024 * 1024))
    with pytest.raises(ValueError, match="expands beyond"):
        combine_uploads([("bomb.xlsx", buf.getvalue())])


def test_xlsx_entities_are_not_expanded():
    import zipfile
    import openpyxl
    assert openpyxl.DEFUSEDXML is True
    buf = io.BytesIO()
    synthetic_df().head(1).to_excel(buf, index=False)
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for info in src.infolist():
            payload = src.read(info)
            if info.filename == "xl/workbook.xml":
                payload = payload.replace(
                    b"<?xml",
                    b"<?xml version=\"1.0\"?><!DOCTYPE foo [<!ENTITY pwned \"EXPANDED_SECRET\">]><?xml",
                    1,
                )
                payload = payload.replace(b"<workbook", b"<workbook>&pwned;<workbook", 1)
            archive.writestr(info, payload)
    with pytest.raises(Exception) as caught:
        combine_uploads([("entity.xlsx", out.getvalue())])
    assert "EXPANDED_SECRET" not in str(caught.value)


def test_engagement_columns_kept_numeric():
    df = synthetic_df()
    df["Likes"] = ["1,234", "5", "", None, "7"]
    df["Reach"] = [100, 200, 300, 400, 500]
    df["Social Echo"] = ["", "", "", "", ""]
    out = flag_posts(df)
    assert out["Likes"].tolist()[:2] == [1234, 5]
    assert pd.api.types.is_numeric_dtype(out["Reach"])
    assert pd.api.types.is_numeric_dtype(out["Social Echo"])
