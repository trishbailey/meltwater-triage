import json
from datetime import datetime, timezone

import pandas as pd

from triage.aggregate import recurring_threat_posters
from triage.filters import Patterns, screen_text
from triage.pipeline import process, to_workbook
from triage.refine import refine
from triage.storage import Storage, rows_to_store

P = Patterns()


# --- noisy terms ---------------------------------------------------------
def test_mp_uppercase_only():
    assert screen_text("We will kill the MP", P)["category"] == "threat"
    assert screen_text("We will kill the mp3 player", P)["category"] == ""
    assert screen_text("kill it, mp", P)["category"] == ""


def test_weak_threat_terms_need_nearby_target():
    far = "Attack on the bowling team was brutal " + "word " * 20 + "and AUKUS is in the news."
    assert screen_text(far, P)["category"] == ""
    assert screen_text("They will attack HMAS Stirling", P)["category"] == "threat"
    assert screen_text("Hang the defence minister", P)["category"] == "threat"
    # strong terms still only need a target anywhere
    assert screen_text("I will bomb it. " + "word " * 20 + "AUKUS", P)["category"] == "threat"


def test_weak_protest_terms_need_other_signal():
    assert screen_text("Register for the training demo outside the office", P)["category"] == ""
    r = screen_text("Join us outside Parliament House, register now #NoAUKUS", P)
    assert r["category"] == "protest"
    assert "protest:register" in r["matched_keywords"]


# --- refine protest fields ----------------------------------------------
def test_refine_protest_fields():
    flagged = pd.DataFrame({"Hit Sentence": ["Rally at Garden Island"], "category": ["protest"],
                            "matched_keywords": ["protest:rally"], "Author Handle": ["@x"]})
    seen = {}

    def fake(system, user):
        seen["payload"] = json.loads(user)["posts"][0]
        return json.dumps({"results": [{"id": 0, "is_genuine": True, "refined_category": "protest",
            "protest_location": "Garden Island, WA", "protest_topic": "nuclear submarines",
            "organizers_or_groups": ["Stop AUKUS WA"], "event_date": "Saturday 9 Nov",
            "scale_or_turnout": "about 200"}]})

    out = refine(flagged, ["Hit Sentence"], complete=fake)
    assert out.loc[0, "protest_location"] == "Garden Island, WA"
    assert out.loc[0, "organizers_or_groups"] == "Stop AUKUS WA"
    assert out.loc[0, "scale_or_turnout"] == "about 200"
    assert seen["payload"]["author"] == "@x"


# --- recurring posters --------------------------------------------------
def test_recurring_threat_posters():
    df = pd.DataFrame({
        "Date": ["2026-10-01", "2026-10-20", "2026-10-05", "2026-10-07", "2026-10-09"],
        "Author Handle": ["@a", "@A", "@b", "@c", "@c"],
        "Author Name": ["Ann", "Ann", "Bob", "Cy", "Cy"],
        "Engagement": [10, 5, 99, 1, 2],
        "category": ["threat", "both", "threat", "protest", "protest"],
    })
    out = recurring_threat_posters(df)
    assert list(out["author_handle"]) == ["@a"]
    assert out.loc[0, "threat_posts"] == 2 and out.loc[0, "total_engagement"] == 15
    assert str(out.loc[0, "first_seen"].date()) == "2026-10-01"
    assert str(out.loc[0, "last_seen"].date()) == "2026-10-20"


# --- storage ------------------------------------------------------------
def _flagged(ids, cat="threat"):
    return pd.DataFrame({"Document ID": ids, "Date": ["2026-10-01"] * len(ids),
                         "Author Handle": ["@a"] * len(ids), "Engagement": [5] * len(ids),
                         "category": [cat] * len(ids), "link": [f"https://e.x/{i}" for i in ids]})


def test_storage_dedupe_and_monthly_by_post_date(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'t.db'}")
    oct_rows = _flagged(['"d1"', "d2"]).assign(Date=["2026-09-30", "2026-10-02"], is_flagged=True)
    st.save_run(oct_rows, ["a.xlsx"], 10, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    nov_rows = _flagged(["d2", "d3"], "protest").assign(Date=["2026-10-02", "2026-11-03"], is_flagged=True)
    st.save_run(nov_rows, ["b.xlsx"], 8, now=datetime(2026, 11, 4, tzinfo=timezone.utc))
    posts = st.load_posts()
    assert len(posts) == 3  # d2 deduped; quotes stripped from d1
    d2 = posts[posts["Document ID"] == "d2"].iloc[0]
    assert d2["post_month"] == "2026-10" and d2["upload_month"] == "2026-10"
    assert d2["category"] == "protest"
    d1 = posts[posts["Document ID"] == '"d1"'].iloc[0]
    assert d1["post_month"] == "2026-09" and d1["post_week"] == "2026-W40"
    m = st.monthly_counts()
    assert m.to_dict("records") == [
        {"month": "2026-09", "category": "threat", "posts": 1, "engagement": 5.0},
        {"month": "2026-10", "category": "protest", "posts": 1, "engagement": 5.0},
        {"month": "2026-11", "category": "protest", "posts": 1, "engagement": 5.0},
    ]
    assert set(st.load_posts("2026-11")["Document ID"]) == {"d3"}


def test_storage_keeps_prior_review_on_reupload(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'h.db'}")
    posts = pd.DataFrame({"Document ID": ["f"], "Date": ["2026-10-01"], "Engagement": [1],
                          "category": ["threat"], "severity": ["high"]})
    st.save_run(rows_to_store(posts, posts), ["w1.xlsx"], 1)
    again = pd.DataFrame({"Document ID": ["f"], "Date": ["2026-10-01"], "category": [""],
                          "is_flagged": [False]})
    st.save_run(again, ["w2.xlsx"], 1)
    f = st.load_posts(flagged_only=True)
    assert list(f["Document ID"]) == ["f"] and f.iloc[0]["severity"] == "high"


def test_storage_merges_seerist_twin_but_not_same_source(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'t.db'}")
    def rows(ids, src):
        return pd.DataFrame({"Document ID": ids, "Date": ["2026-10-01"] * len(ids), "category": ["threat"] * len(ids),
                             "link": ["https://e.x/a"] * len(ids), "export_source": [src] * len(ids)})
    st.save_run(rows(["m1", "m2"], "Meltwater"), ["a.xlsx"], 2)
    assert len(st.load_posts()) == 2  # same URL, same source: distinct posts
    st.save_run(rows(["seerist:1"], "Seerist"), ["b.pdf"], 1)
    assert len(st.load_posts()) == 2  # Seerist twin merged into an existing post


def test_pipeline_end_to_end(tmp_path, monkeypatch):
    import io
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    df = pd.DataFrame({"Document ID": ["1", "2", "3"], "Author Handle": ["@a", "@a", "@b"],
                       "Engagement": ["1,000", "2", "3"], "Hit Sentence": [
                           "We will bomb the AUKUS shipyard", "Kill the MP", "Lovely day"]})
    buf = io.BytesIO(); df.to_excel(buf, index=False)
    st = Storage(f"sqlite:///{tmp_path/'p.db'}")
    calls = []
    res = process([("w.xlsx", buf.getvalue())], refine_fn=lambda s, u: '{"results": []}',
                  storage=st)
    assert len(res.flagged) == 2 and res.run_id == 1
    assert res.posters.loc[0, "author_handle"] == "@a" and res.posters.loc[0, "total_engagement"] == 1002
    sheets = pd.ExcelFile(io.BytesIO(to_workbook(res))).sheet_names
    assert sheets == ["Warnings", "Threat posts", "Protest posts", "Recurring threat posters"]  # warning: nothing reviewed
    assert len(st.load_posts()) == 2  # only flagged posts are kept
    assert len(st.load_posts(flagged_only=True)) == 2
    assert "__triage_src_row" not in st.load_posts().columns


def test_workbook_neutralizes_formulas_and_bad_chars(monkeypatch):
    import io
    import zipfile
    from openpyxl import load_workbook
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    df = pd.DataFrame({
        "Hit Sentence": ['=HYPERLINK("http://evil","x") bomb AUKUS\x07', "calm"],
        "Author Name": ["+cmd|'/c calc'!A0", "ok"],
        "\x07=Name": ["@keep", "x"],
    })
    res = process([("f.csv", df.to_csv(index=False).encode())])  # CSV can carry control chars
    raw = to_workbook(res)
    sheet = zipfile.ZipFile(io.BytesIO(raw)).read("xl/worksheets/sheet1.xml")
    assert b"<f>" not in sheet and b"<f " not in sheet
    wb = load_workbook(io.BytesIO(raw))
    ws = wb["Threat posts"]
    types = {c.data_type for row in ws.iter_rows() for c in row}
    values = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
    assert "f" not in types
    assert any(v.startswith("=HYPERLINK") for v in values)
    assert any(v.startswith("+cmd|") for v in values)
    assert "@keep" in values
    assert any(v.startswith("=Name") for v in values)


def test_storage_failure_still_returns_results(monkeypatch):
    import io
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    class Broken:
        def save_run(self, *a, **k):
            raise RuntimeError("db down")

    buf = io.BytesIO(); pd.DataFrame({"Hit Sentence": ["kill the defence minister"]}).to_excel(buf, index=False)
    res = process([("f.xlsx", buf.getvalue())], storage=Broken())
    assert len(res.flagged) == 1 and res.run_id is None and res.notes
    warn = pd.read_excel(io.BytesIO(to_workbook(res)), sheet_name="Warnings")
    assert "not saved" in str(warn.iloc[0, 0]).lower()


def _cap_file():
    import io
    df = pd.DataFrame({
        "Document ID": ["1", "2", "3", "4"],
        "Engagement": [1, 100, 50, 3],
        "Hit Sentence": ["kill the defence minister", "bomb HMAS Stirling", "nice weather", "lovely day"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _cap_fake(calls):
    import json

    def fake(system, user):
        posts = json.loads(user)["posts"]
        kind = "refine"
        calls.append((kind, len(posts), [p.get("text") for p in posts]))
        return json.dumps({"results": [
            {"id": p["id"], "is_genuine": True, "refined_category": "threat", "severity": "low"}
            for p in posts]})

    return fake


def test_monthly_cap_prefers_review_then_stops(tmp_path, monkeypatch):
    import io
    monkeypatch.setenv("ANTHROPIC_MONTHLY_POST_CAP", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PERSIST_RUNS", raising=False)
    st = Storage(f"sqlite:///{tmp_path/'cap.db'}")
    calls = []
    data = _cap_file()
    res = process([("f.xlsx", data)], refine_fn=_cap_fake(calls), storage=st)
    assert calls[0][0] == "refine" and calls[0][1] == 1
    assert st.posts_sent_this_month() == 1
    assert len(res.flagged) == 2
    calls.clear()
    res2 = process([("f.xlsx", data)], refine_fn=_cap_fake(calls), storage=st)
    assert calls == []
    assert len(res2.flagged) == 2
    assert st.posts_sent_this_month() == 1
    warn = pd.read_excel(io.BytesIO(to_workbook(res2)), sheet_name="Warnings")
    text = " ".join(warn["warning"].astype(str))
    assert "monthly limit" in text and "Keyword screening" in text


def test_monthly_cap_requires_a_database(monkeypatch):
    import io
    monkeypatch.setenv("ANTHROPIC_MONTHLY_POST_CAP", "50")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PERSIST_RUNS", raising=False)
    calls = []
    res = process([("f.xlsx", _cap_file())], refine_fn=_cap_fake(calls))
    assert calls == []
    assert len(res.flagged) == 2
    warn = pd.read_excel(io.BytesIO(to_workbook(res)), sheet_name="Warnings")
    assert "no database" in str(warn.iloc[0, 0]).lower()


def test_invalid_monthly_cap_skips_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MONTHLY_POST_CAP", "lots")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = Storage(f"sqlite:///{tmp_path/'bad.db'}")
    calls = []
    res = process([("f.xlsx", _cap_file())], refine_fn=_cap_fake(calls), storage=st)
    assert calls == [] and res.notes
    assert "ANTHROPIC_MONTHLY_POST_CAP" in res.notes[0]
    assert st.posts_sent_this_month() == 0


def test_usage_resets_next_month(tmp_path):
    from datetime import datetime, timezone
    st = Storage(f"sqlite:///{tmp_path/'m.db'}")
    may = datetime(2026, 5, 31, tzinfo=timezone.utc)
    june = datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert st.reserve_monthly_posts(2, 2, now=may) == (2, 2)
    assert st.reserve_monthly_posts(1, 2, now=may) == (0, 2)
    assert st.reserve_monthly_posts(1, 2, now=june) == (1, 1)
    assert st.posts_sent_this_month(may) == 2
    assert st.posts_sent_this_month(june) == 1


def test_reserve_does_not_exceed_cap_under_contention(tmp_path):
    import threading
    from datetime import datetime, timezone
    st = Storage(f"sqlite:///{tmp_path/'race.db'}")
    now = datetime(2026, 4, 1, tzinfo=timezone.utc)
    results = []

    def worker():
        results.append(st.reserve_monthly_posts(1, 5, now=now)[0])

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 5
    assert st.posts_sent_this_month(now) == 5


def test_document_ids_dedupe_across_excel_floats(tmp_path):
    st = Storage(f"sqlite:///{tmp_path/'id.db'}")
    rows = pd.DataFrame({
        "Document ID": [12345.0, "12345", "  ", '"'],
        "Hit Sentence": ["kill the senator one", "kill the senator two", "bomb HMAS Stirling", "attack the defence minister"],
        "Date": ["2026-10-01"] * 4,
        "category": ["threat"] * 4,
        "is_flagged": [True] * 4,
    })
    st.save_run(rows, ["a.xlsx"], 4)
    assert len(st.load_posts()) == 3


def test_excel_blank_does_not_float_document_id(tmp_path, monkeypatch):
    import io
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    df = pd.DataFrame({
        "Document ID": [12345, None],
        "Hit Sentence": ["kill the defence minister", "bomb HMAS Stirling"],
        "Date": ["2026-10-01", "2026-10-02"],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    st = Storage(f"sqlite:///{tmp_path/'xlsx.db'}")
    process([("a.xlsx", buf.getvalue())], storage=st)
    ids = {str(v) for v in st.load_posts()["Document ID"].tolist()}
    assert "12345" in ids
    assert "12345.0" not in ids


def test_postgres_url_uses_psycopg_driver(monkeypatch):
    from sqlalchemy import create_engine
    from triage.storage import database_url, normalize_database_url
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db.internal:5432/triage?sslmode=require")
    url = database_url()
    assert url == "postgresql+psycopg://user:pw@db.internal:5432/triage?sslmode=require"
    assert normalize_database_url("postgresql+psycopg2://u:pw@h/db") == "postgresql+psycopg2://u:pw@h/db"
    engine = create_engine(url)
    assert engine.dialect.driver == "psycopg"
    engine.dispose()


def test_anthropic_call_uses_supported_args(monkeypatch):
    """Regression: SDK 2.x rejects temperature; only pass supported arguments."""
    import inspect
    import anthropic
    from anthropic.resources.messages import Messages
    from triage.refine import anthropic_complete

    allowed = set(inspect.signature(Messages.create).parameters)
    seen = {}

    class FakeMessages:
        def create(self, **kwargs):
            seen.update(kwargs)
            block = type("B", (), {"type": "text", "text": '{"results": []}'})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn"})()

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    assert anthropic_complete()("sys", "{}") == '{"results": []}'
    assert set(seen) <= allowed
