"""Migrations, resumable jobs, chunked reads, source labels, and backfill pricing."""
from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from triage.backfill import estimate_refine_cost, execute, plan
from triage.origin import backfill_sources, origin_label


def _book(rows):
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    return buf.getvalue()


def test_origin_never_names_the_export_tool():
    from triage.origin import publisher_label
    assert origin_label({"Source": "Meltwater", "URL": "https://www.twitter.com/a/status/9"}) == "X"
    assert origin_label({"Source": "News", "URL": "https://www.abc.net.au/news/story"}) == "abc.net.au"
    # A social URL is the platform. The outlet name stays on the publisher, not the source.
    assert origin_label({"Source Name": "The Guardian", "URL": "https://x.com/a/1"}) == "X"
    assert publisher_label({"Source Name": "The Guardian", "URL": "https://x.com/a/1"}) == "The Guardian"
    assert origin_label({"Source": "The Guardian", "URL": "https://www.theguardian.com/world/story"}) == "The Guardian"
    assert origin_label({"Source Name": "ABC News", "URL": "https://www.abc.net.au/news/1"}) == "ABC News"
    assert "meltwater" not in origin_label({"Source": "Meltwater"}).lower()
    assert origin_label({}) == "Unknown source"


def test_source_backfill_keeps_rows_and_real_names(tmp_path):
    from triage.storage import Storage, posts
    st = Storage(f"sqlite:///{tmp_path/'s.db'}")
    now = datetime.now(timezone.utc)
    blob = {"Document ID": "p1", "URL": "https://x.com/a/status/1", "Hit Sentence": "hello",
            "Source": "Meltwater", "Date": "2026-10-01", "category": "threat", "is_flagged": True}
    with st.engine.begin() as conn:
        conn.execute(posts.insert().values(
            document_id="p1", first_run_id=1, last_run_id=1, upload_week="2026-W40",
            upload_month="2026-10", is_flagged=True, category="threat",
            source_name="Meltwater", link="https://x.com/a/status/1", data=blob,
            post_month="2026-10"))
        # A real outlet name must survive.
        blob2 = dict(blob, **{"Document ID": "p2", "Source": "ABC News",
                              "URL": "https://www.abc.net.au/x", "link": "https://www.abc.net.au/x"})
        conn.execute(posts.insert().values(
            document_id="p2", first_run_id=1, last_run_id=1, upload_week="2026-W40",
            upload_month="2026-10", is_flagged=True, category="protest",
            source_name="ABC News", link="https://www.abc.net.au/x", data=blob2,
            post_month="2026-10"))
    from sqlalchemy import select
    before = st.load_posts()
    n = backfill_sources(st)
    after = st.load_posts()
    assert len(after) == len(before) == 2
    assert n >= 1
    with st.engine.connect() as conn:
        saved = {r.document_id: r.source_name for r in conn.execute(
            select(posts.c.document_id, posts.c.source_name))}
    assert saved["p1"] == "X"
    assert saved["p2"] == "ABC News"
    assert "meltwater" not in saved["p1"].lower()


def test_chunks_cover_the_sheet_without_one_giant_read(monkeypatch):
    monkeypatch.setenv("SCREEN_CHUNK_ROWS", "2")
    from triage.filters import iter_upload_chunks
    rows = [{"Document ID": f"d{i}", "Date": "2026-11-01", "Hit Sentence": f"line {i}",
             "Engagement": i, "URL": f"https://x.com/a/status/{i}"} for i in range(5)]
    chunks = list(iter_upload_chunks([("a.xlsx", _book(rows))]))
    assert len(chunks) >= 3
    assert all(len(c) <= 2 for c in chunks)
    got = pd.concat(chunks, ignore_index=True)
    assert got["Document ID"].tolist() == [f"d{i}" for i in range(5)]
    assert got["Hit Sentence"].tolist() == [f"line {i}" for i in range(5)]


def test_legacy_job_table_migrates_and_status_loads(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("PERSIST_RUNS", raising=False)
    eng = create_engine(url)
    now = datetime.now(timezone.utc).isoformat()
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE upload_jobs (
                id VARCHAR(64) PRIMARY KEY,
                kind VARCHAR(32) NOT NULL,
                state VARCHAR(16) NOT NULL,
                stage TEXT NOT NULL,
                done INTEGER NOT NULL,
                total INTEGER NOT NULL,
                message TEXT NOT NULL,
                error TEXT NOT NULL,
                filenames TEXT NOT NULL,
                rows INTEGER NOT NULL,
                added INTEGER,
                skipped INTEGER,
                items INTEGER,
                has_download INTEGER NOT NULL,
                started DATETIME NOT NULL,
                updated DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO upload_jobs
            (id, kind, state, stage, done, total, message, error, filenames, rows, has_download, started, updated)
            VALUES ('legacy1', 'screen', 'done', 'Saving', 5, 5, 'kept', '', 'a.xlsx', 5, 0, :s, :u)
        """), {"s": now, "u": now})
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    row = jobs.get("legacy1")
    assert row is not None and row["state"] == "done" and row["message"] == "kept"
    with jobs._eng().connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(upload_jobs)"))}
    assert "payload" in cols and "checkpoint" in cols and "resume_token" in cols
    jobs._update_db("legacy1", {"state": "done", "message": "kept"}, payload=b"excel-bytes")
    assert jobs.payload("legacy1") == b"excel-bytes"
    # Status read still does not require the caller to load the blob to learn state.
    again = jobs.get("legacy1")
    assert again["state"] == "done" and "payload" not in again


def test_running_job_resumes_from_stored_bytes(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'resume.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs.JOB_DIR = tmp_path / "jobs"
    jobs._worker_id = "this-process"
    now = datetime.now(timezone.utc)
    with jobs._eng().begin() as conn:
        conn.execute(jobs.jobs_table.insert().values(
            id="run1", kind="screen", state="running", stage="Reviewing flagged posts",
            done=10, total=100, message="", error="", filenames="big.xlsx", rows=100,
            has_download=0, started=now, updated=now, resume_token="dead-process"))
        conn.execute(jobs.job_inputs.insert().values(
            job_id="run1", seq=0, filename="big.xlsx", body=b"stored-upload"))
    seen = {}

    def factory(files):
        def work(progress):
            seen["files"] = files
            progress("Saving for the dashboard", 1, 1)
            return {"payload": b"out-bytes", "rows": 100, "message": "resumed"}
        return work

    stats = jobs.resume_pending(factory)
    assert stats["resumed"] == 1 and stats["interrupted"] == 0
    for _ in range(50):
        state = jobs.get("run1")["state"]
        if state != "running":
            break
        time.sleep(0.05)
    assert jobs.get("run1")["state"] == "done"
    assert jobs.payload("run1") == b"out-bytes"
    assert seen["files"] == [("big.xlsx", b"stored-upload")]


def test_running_job_without_bytes_is_interrupted(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'orphan.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs._worker_id = "this-process"
    now = datetime.now(timezone.utc)
    with jobs._eng().begin() as conn:
        conn.execute(jobs.jobs_table.insert().values(
            id="gone", kind="screen", state="running", stage="Reading file",
            done=0, total=0, message="", error="", filenames="x.xlsx", rows=0,
            has_download=0, started=now, updated=now, resume_token=None))
    stats = jobs.resume_pending(lambda files: (lambda progress: {}))
    assert stats["interrupted"] == 1 and stats["resumed"] == 0
    assert jobs.get("gone")["state"] == "error"
    assert "upload again" in jobs.get("gone")["error"]


def test_backfill_price_is_estimate_only_and_blocks_over_five_dollars(tmp_path):
    small = estimate_refine_cost(1000)
    mid = estimate_refine_cost(4000)
    large = estimate_refine_cost(10000)
    assert small["usd"] < 5 and mid["usd"] < 5
    assert large["over_limit"] and large["usd"] > 5
    assert small["input_usd_per_mtok"] == 1 and small["output_usd_per_mtok"] == 5
    from triage.storage import Storage
    st = Storage(f"sqlite:///{tmp_path/'b.db'}")
    report = execute(st, approve=False)
    assert report["ran"] is False
    assert "approval" in report["reason"].lower()
    priced = plan(st)
    assert priced["will_run"] is False and priced["posts"] == 0


def test_flagged_backfill_merges_review_without_deleting(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from triage.pipeline import backfill_flagged
    from triage.storage import Storage, rows_to_store
    st = Storage(f"sqlite:///{tmp_path/'f.db'}")
    df = pd.DataFrame([
        {"Document ID": "keep", "Date": "2026-10-02", "URL": "https://x.com/a/1",
         "Hit Sentence": "bomb the base", "category": "threat", "is_flagged": True,
         "Engagement": 3},
        {"Document ID": "done", "Date": "2026-10-03", "URL": "https://x.com/a/2",
         "Hit Sentence": "already reviewed", "category": "threat", "is_flagged": True,
         "refined_category": "threat", "Engagement": 1},
    ])
    st.save_run(rows_to_store(df, df), ["s.xlsx"], 2)

    def fake(system, user):
        import json
        posts = json.loads(user)["posts"]
        return json.dumps({"results": [
            {"id": p["id"], "is_genuine": True, "refined_category": "threat",
             "severity": "low", "threat_type": "violence", "targets": ["HMAS Stirling"],
             "narrative_summary": "reviewed"} for p in posts]})

    n = backfill_flagged(st, complete=fake)
    assert n == 1
    loaded = st.load_posts()
    assert set(loaded["Document ID"]) == {"keep", "done"}
    by = loaded.set_index("Document ID")
    assert by.loc["keep", "refined_category"] == "threat"
    assert by.loc["done", "narrative_summary"] != "reviewed"


def test_map_layers_and_grouping(monkeypatch):
    monkeypatch.setattr("triage.geo.persistence_enabled", lambda: False)
    monkeypatch.setattr("triage.geo._nominatim", lambda place: None)
    from triage import dashboard as dash
    from triage.mapdata import build_map
    future = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
    posted = datetime.now(timezone.utc) - timedelta(days=2)
    posts = pd.DataFrame([
        {"Document ID": "t1", "Date": posted.strftime("%Y-%m-%d"), "URL": "https://x.com/a/9",
         "post_date": pd.Timestamp(posted), "post_month": posted.strftime("%Y-%m"),
         "category": "threat", "is_flagged": True, "threat_type": "violence",
         "targets": "Perth", "Engagement": 4, "Hit Sentence": "bomb the base",
         "is_genuine": True},
        {"Document ID": "p1", "Date": future, "URL": "https://facebook.com/events/1",
         "post_date": pd.Timestamp("2026-11-02", tz="UTC"), "post_month": "2026-11",
         "category": "protest", "is_flagged": True, "protest_location": "Perth",
         "event_date": future, "Hit Sentence": "rally on the date", "Engagement": 2,
         "mobilization_status": "Mobilization candidate: action/CTA plus date, time, or place"},
        {"Document ID": "a1", "Date": "2026-11-03", "URL": "https://example.com/say",
         "post_date": pd.Timestamp("2026-11-03", tz="UTC"), "post_month": "2026-11",
         "category": "protest", "is_flagged": True, "protest_location": "Canberra",
         "Hit Sentence": "send a submission", "Engagement": 1,
         "mobilization_status": "Advocacy: inquiry/submission campaign",
         "narrative_summary": float("nan")},
    ])
    mapped = build_map(posts, None, days=None)
    kinds = {e["kind"] for e in mapped["events"]}
    assert "planned" in kinds and "advocacy" not in kinds
    assert len(posts) == 3
    assert mapped["threats"] and mapped["threats"][0]["source"] != "Meltwater"
    assert all("meltwater" not in (e.get("source") or "").lower() for e in mapped["events"])
    assert all(e["source"] for e in mapped["events"])
    ctx = dash.build(posts, None, month="2026-11")
    assert "stated target" in ctx["threat_chart"]["grouping"].lower()
    assert "unspecified target" not in " ".join(row["label"].lower() for row in ctx["threat_chart"]["rows"])
    assert ctx["upcoming"] and ctx["upcoming"][0]["place"] == "Perth"
    assert "meltwater" not in ctx["upcoming"][0]["source"].lower()
    assert ctx["narrative"]
    assert ctx["flagged_sample"]
    assert "meltwater" not in ctx["flagged_sample"][0]["source"].lower()


def test_large_screen_drops_unneeded_rows_but_counts_them(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREEN_KEEP_ROWS", "3")
    monkeypatch.setenv("HEAT_MAX_POSTS", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from triage.pipeline import process
    rows = []
    for i in range(6):
        text = "bomb HMAS Stirling tomorrow" if i == 0 else f"weather is fine today number {i}"
        rows.append({"Document ID": f"r{i}", "Date": "2026-11-01", "Engagement": 100 - i,
                     "Hit Sentence": text, "URL": f"https://x.com/a/{i}"})
    res = process([("big.xlsx", _book(rows))],
                  refine_fn=lambda s, u: '{"results": []}',
                  heat_fn=lambda s, u: '{"results": []}')
    assert res.screened_rows == 6
    assert len(res.all_posts) < 6
    assert len(res.flagged) == 1


def test_screen_bounds_retained_rows_per_chunk(monkeypatch):
    monkeypatch.setenv("SCREEN_CHUNK_ROWS", "2")
    monkeypatch.setenv("SCREEN_KEEP_ROWS", "3")
    monkeypatch.setenv("HEAT_MAX_POSTS", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from triage import pipeline
    rows = []
    for i in range(8):
        text = ("bomb HMAS Stirling tomorrow at the gate" if i == 0
                else f"the weather is calm and quiet today item {i} outside")
        rows.append({"Document ID": f"m{i}", "Date": "2026-11-01", "Engagement": 20 - i,
                     "Hit Sentence": text, "URL": f"https://x.com/a/{i}"})
    pipeline._peak_retained["n"] = 0
    res = pipeline.process([("c.xlsx", _book(rows))],
                           refine_fn=lambda s, u: '{"results": []}',
                           heat_fn=lambda s, u: '{"results": []}')
    assert res.screened_rows == 8
    assert pipeline._peak_retained["n"] < 8
    assert pipeline._peak_retained["n"] <= 3 + 2


def test_crash_after_paid_batch_is_not_repeated(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    monkeypatch.setenv("REFINE_BATCH_SIZE", "1")
    monkeypatch.setenv("HEAT_MAX_POSTS", "0")
    from triage.pipeline import process
    first = "kill the AUKUS deal tomorrow at the gate"
    second = "bomb HMAS Stirling next week in the yard"
    data = _book([
        {"Document ID": "P1", "URL": "https://x.com/a/1", "Engagement": 9, "Hit Sentence": first},
        {"Document ID": "P2", "URL": "https://x.com/b/2", "Engagement": 8, "Hit Sentence": second},
    ])

    class Store:
        def __init__(self):
            self.rows = []

        def load(self):
            return list(self.rows)

        def save(self, kind, records):
            self.rows.extend(records)

    store = Store()
    calls = []

    def fake(system, user):
        calls.append(user)
        posts = __import__("json").loads(user)["posts"]
        return __import__("json").dumps({"results": [
            {"id": p["id"], "is_genuine": True, "refined_category": "threat",
             "severity": "low", "threat_type": "violence", "targets": ["AUKUS"],
             "narrative_summary": "kept"} for p in posts]})

    def progress(stage, done, total, **_extra):
        if stage == "Reviewing flagged posts" and done > 0:
            assert store.rows, "progress ran before the paid batch was saved"
            raise RuntimeError("crash after paid batch")

    with pytest.raises(RuntimeError, match="crash after paid batch"):
        process([("a.xlsx", data)], refine_fn=fake, heat_fn=fake, progress=progress, checkpoint=store)
    assert len(calls) == 1
    assert first in calls[0]
    assert any(r.get("document_id") == "P1" for r in store.rows)
    calls.clear()
    process([("a.xlsx", data)], refine_fn=fake, heat_fn=fake, checkpoint=store)
    assert calls, "the unpaid post should still be reviewed"
    assert all(first not in user for user in calls)
    assert any(second in user for user in calls)


def test_save_failed_job_resumes_without_paying_again(tmp_path, monkeypatch):
    """A save failure keeps the paid batches, and the resumed job does not buy them twice."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_MONTHLY_POST_CAP", raising=False)
    monkeypatch.setenv("REFINE_BATCH_SIZE", "1")
    monkeypatch.setenv("HEAT_MAX_POSTS", "0")
    url = f"sqlite:///{tmp_path/'again.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    from triage.pipeline import SaveFailed, process
    from triage.storage import Storage
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs.JOB_DIR = tmp_path / "jobs"
    first = "kill the AUKUS deal tomorrow at the gate"
    second = "bomb HMAS Stirling next week in the yard"
    data = _book([
        {"Document ID": "P1", "URL": "https://x.com/a/1", "Engagement": 9, "Hit Sentence": first},
        {"Document ID": "P2", "URL": "https://x.com/b/2", "Engagement": 8, "Hit Sentence": second},
    ])
    inner = Storage(url)

    class Flaky:
        def __init__(self):
            self.saves = 0

        def reuse_index(self, *args, **kwargs):
            return inner.reuse_index(*args, **kwargs)

        def save_run(self, *args, **kwargs):
            self.saves += 1
            if self.saves == 1:
                raise RuntimeError("db down")
            return inner.save_run(*args, **kwargs)

    flaky = Flaky()
    calls = []

    def fake(system, user):
        calls.append(user)
        posts = __import__("json").loads(user)["posts"]
        return __import__("json").dumps({"results": [
            {"id": p["id"], "is_genuine": True, "refined_category": "threat",
             "severity": "low", "threat_type": "violence", "targets": ["AUKUS"],
             "narrative_summary": "kept"} for p in posts]})

    def work(progress):
        result = process([("a.xlsx", data)], refine_fn=fake, heat_fn=fake, storage=flaky,
                         progress=progress, checkpoint=jobs.JobCheckpoint(jobs.current_jid()))
        if result.saved is False:
            raise SaveFailed("Results were not saved to the database. Completed reviews were kept.")
        return {"payload": b"ok", "rows": result.screened_rows, "message": "saved"}

    jid = jobs.start("screen", work, filenames="a.xlsx", inputs=[("a.xlsx", data)])
    for _ in range(80):
        got = jobs.get(jid)
        if got and got["state"] == "error":
            break
        time.sleep(0.05)
    assert jobs.get(jid)["state"] == "error"
    assert jobs.get(jid)["stage"] == jobs.SAVE_FAILED_STAGE
    assert any(first in c for c in calls) and any(second in c for c in calls)
    paid = list(calls)
    saved_ids = {row.get("document_id") for row in jobs.load_batches(jid)}
    assert saved_ids >= {"P1", "P2"}
    calls.clear()
    from sqlalchemy import select
    stored = None
    for _ in range(40):
        with jobs._eng().connect() as conn:
            stored = conn.execute(
                select(jobs.jobs_table.c.state, jobs.jobs_table.c.stage).where(jobs.jobs_table.c.id == jid)
            ).first()
        if stored and stored[0] == "error" and stored[1] == jobs.SAVE_FAILED_STAGE:
            break
        time.sleep(0.05)
    assert stored and stored[0] == "error" and stored[1] == jobs.SAVE_FAILED_STAGE
    stats = jobs.resume_pending(lambda files: work)
    assert stats["resumed"] == 1
    for _ in range(80):
        if jobs.get(jid)["state"] == "done":
            break
        time.sleep(0.05)
    assert jobs.get(jid)["state"] == "done", jobs.get(jid)
    assert calls == [], calls
    assert flaky.saves == 2
    assert set(inner.load_posts()["Document ID"]) >= {"P1", "P2"}
    assert paid  # the first pass really was charged


def test_checkpoint_sequence_is_unique_when_batches_race(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'seq.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs._worker_id = "owner"
    now = datetime.now(timezone.utc)
    with jobs._eng().begin() as conn:
        conn.execute(jobs.jobs_table.insert().values(
            id="c1", kind="screen", state="running", stage="Reviewing flagged posts",
            done=0, total=8, message="", error="", filenames="a.xlsx", rows=8,
            has_download=0, started=now, updated=now, resume_token="owner",
            lease_until=now))
    errors = []

    def add(i):
        try:
            jobs.append_batch("c1", "refine", [{
                "document_id": f"d{i}", "refined_category": "threat", "narrative_summary": "kept",
            }])
        except Exception as exc:
            errors.append(exc)

    threads = [__import__("threading").Thread(target=add, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [], errors
    rows = jobs.load_batches("c1")
    assert len(rows) == 8
    with jobs._eng().connect() as conn:
        seqs = [r[0] for r in conn.execute(text(
            "SELECT seq FROM upload_job_checkpoints WHERE job_id='c1' ORDER BY seq"))]
    assert seqs == list(range(1, 9))


def test_save_failure_keeps_recovery_inputs(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'save.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("JOB_DIR", str(tmp_path / "jobs"))
    from triage.pipeline import SaveFailed
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs.JOB_DIR = tmp_path / "jobs"

    def work(progress):
        progress("Reviewing flagged posts", 1, 1)
        raise SaveFailed("Results were not saved to the database. Completed reviews were kept.")

    jid = jobs.start("screen", work, filenames="a.xlsx", inputs=[("a.xlsx", b"excel-bytes")])
    for _ in range(50):
        state = jobs.get(jid)["state"]
        if state != "running":
            break
        time.sleep(0.05)
    got = jobs.get(jid)
    assert got["state"] == "error"
    assert "not saved" in got["error"].lower()
    assert jobs._load_inputs(jid) == [("a.xlsx", b"excel-bytes")]


def test_input_store_failure_does_not_start_work(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'nostore.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs._eng()
    with jobs._eng().begin() as conn:
        conn.execute(text("DROP TABLE upload_job_inputs"))
    with pytest.raises(jobs.InputNotStored):
        jobs._store_inputs("missing", [("a.xlsx", b"abc")])
    calls = []

    def work(progress):
        calls.append("started")
        return {"payload": b"nope"}

    def broken(jid, files):
        raise jobs.InputNotStored("db down")

    monkeypatch.setattr(jobs, "_store_inputs", broken)
    with pytest.raises(jobs.InputNotStored):
        jobs.start("screen", work, filenames="a.xlsx", inputs=[("a.xlsx", b"abc")])
    time.sleep(0.15)
    assert calls == []


def test_live_lease_is_not_claimed_and_old_writes_are_fenced(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path/'lease.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    import triage.jobs as jobs
    jobs._engine = None
    jobs._engine_url = ""
    jobs._jobs.clear()
    jobs._worker_id = "worker-a"
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with jobs._eng().begin() as conn:
        conn.execute(jobs.jobs_table.insert().values(
            id="live", kind="screen", state="running", stage="Reviewing flagged posts",
            done=1, total=4, message="", error="", filenames="a.xlsx", rows=4,
            has_download=0, started=past, updated=datetime.now(timezone.utc),
            resume_token="worker-b", lease_until=future))
        conn.execute(jobs.job_inputs.insert().values(
            job_id="live", seq=0, filename="a.xlsx", body=b"file"))
    started = []
    stats = jobs.resume_pending(lambda files: (lambda progress: started.append(files) or {}))
    assert stats == {"resumed": 0, "interrupted": 0}
    assert started == []
    assert jobs._update_db("live", {"stage": "stolen by a"}) is False
    with jobs._eng().connect() as conn:
        stage = conn.execute(text("SELECT stage FROM upload_jobs WHERE id='live'")).scalar()
    assert stage == "Reviewing flagged posts"
    # The lease has expired, so this worker may claim it, and only that token can write.
    with jobs._eng().begin() as conn:
        conn.execute(text("UPDATE upload_jobs SET lease_until = :past WHERE id='live'"),
                     {"past": past.isoformat()})
    stats = jobs.resume_pending(lambda files: (lambda progress: {"payload": b"ok", "message": "resumed"}))
    assert stats["resumed"] == 1
    for _ in range(40):
        if jobs.get("live")["state"] != "running":
            break
        time.sleep(0.05)
    jobs._jobs.pop("live", None)
    jobs._worker_id = "worker-b"
    assert jobs._update_db("live", {"stage": "old worker write", "state": "error", "error": "nope"}) is False
    with jobs._eng().connect() as conn:
        stage = conn.execute(text("SELECT stage, resume_token FROM upload_jobs WHERE id='live'")).one()
    assert stage[0] != "old worker write"
    assert stage[1] == "worker-a"
