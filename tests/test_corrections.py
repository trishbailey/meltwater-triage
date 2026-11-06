"""Regressions for the review corrections. Fixtures use the reported wording."""
from datetime import datetime, timedelta, timezone

import pandas as pd

from triage.filters import Patterns, screen_text
from triage.geo import geocode, in_australia
from triage.textnorm import collapse_exact, display_handle, single_post_text, usable_source_url

P = Patterns()
SARCASM = (
    "The old bomb the Capital of your ally trick. Should give Canberrans something "
    "to think about at the next AUKUS meeting."
)
SPEND = (
    "@JEChalmers @JEChalmers I know how you can save money. Spend another "
    "$400,000,000,000 on AUKUS so trump can bomb schoolgirls in Iran."
)
GREENS = "The Greens warn that US bases put Perth at risk."
SATURDAY = "This Saturday 7 Nov, Supreme Court Gardens — join us"
DIGEST = (
    "BestFreeToursSydney weekly news: AUKUS headlines, protests in London, budget talks, "
    "and school-holiday tour ideas. Watch our latest YouTube roundup and subscribe."
)
DIRECT = "I will bomb the AUKUS submarine base at HMAS Stirling tomorrow morning."


def test_verbatim_false_flags_are_not_threats_or_protests():
    assert screen_text(SARCASM, P)["category"] == ""
    assert "third_party_or_sarcasm" in screen_text(SARCASM, P)["judgment_reason"]
    spend = screen_text(SPEND, P)
    assert spend["category"] == ""
    assert "third_party_or_sarcasm" in spend["judgment_reason"]
    greens = screen_text(GREENS, P)
    assert greens["category"] == ""
    assert "policy_warning" in greens["judgment_reason"]
    digest = screen_text(DIGEST, P, author="BestFreeToursSydney")
    assert digest["category"] == ""
    assert "news_or_promotion" in digest["judgment_reason"]
    assert screen_text("Make a submission on the AUKUS inquiry", P)["category"] == ""


def test_saturday_supreme_court_gardens_is_a_protest_in_perth(monkeypatch):
    monkeypatch.setattr("triage.geo.persistence_enabled", lambda: False)
    monkeypatch.setattr("triage.geo._nominatim", lambda place: None)
    hit = screen_text(SATURDAY, P)
    assert hit["category"] == "protest"
    assert hit["high_signal_protest"] is True
    assert hit["protest_location"] == "Supreme Court Gardens"
    assert hit["event_date"].endswith("-11-07")
    place = geocode("Supreme Court Gardens")
    assert place and in_australia(place["lat"], place["lon"])
    assert abs(place["lat"] + 31.95) < 0.05


def test_exact_duplicate_text_and_mention_prefix_collapse_once():
    assert collapse_exact(SPEND).count("@JEChalmers") == 1
    doubled = SPEND + "\n\n" + SPEND
    assert collapse_exact(doubled) == collapse_exact(SPEND)
    row = {"Hit Sentence": DIRECT, "Opening Text": DIRECT, "Title": DIRECT}
    body = single_post_text(row)
    assert body.count("I will bomb the AUKUS submarine base") == 1
    # A reply that is not the same text stays.
    reply = {"Hit Sentence": "The budget paper is out.", "Opening Text": "I still disagree with the timing."}
    both = single_post_text(reply)
    assert "budget paper" in both and "disagree" in both


def test_links_use_a_stored_permalink_or_nothing():
    assert usable_source_url("Supreme Court Gardens") == ""
    assert usable_source_url("none") == ""
    assert usable_source_url("nan") == ""
    kept = usable_source_url("https://bsky.app/profile/perth.bsky.social/post/abc")
    assert kept.startswith("https://bsky.app/")
    assert usable_source_url("bsky.app/profile/perth.bsky.social/post/abc") == (
        "https://bsky.app/profile/perth.bsky.social/post/abc"
    )
    assert display_handle({"Author Handle": "Unknown", "URL": "https://x.com/actualuser/status/9"}) == "@actualuser"
    assert display_handle({"Author Name": "Unknown", "URL": "https://www.youtube.com/watch?v=abc"}) == ""
    assert display_handle({"URL": "https://www.youtube.com/@BestFreeToursSydney/videos"}) == "@BestFreeToursSydney"


def test_recurring_needs_two_distinct_threats():
    from triage.aggregate import recurring_threat_posters
    same = pd.DataFrame({
        "Date": ["2026-11-01", "2026-11-02"],
        "Author Handle": ["@a", "@a"],
        "Author Name": ["Ann", "Ann"],
        "Engagement": [1, 1],
        "category": ["threat", "threat"],
        "Hit Sentence": [DIRECT, DIRECT + " " + DIRECT],
    })
    assert recurring_threat_posters(same).empty
    distinct = same.copy()
    distinct.loc[1, "Hit Sentence"] = "We will attack HMAS Stirling next week and blow the gate."
    out = recurring_threat_posters(distinct)
    assert list(out["author_handle"]) == ["@a"]
    assert int(out.loc[0, "threat_posts"]) == 2


def test_target_synonyms_and_unspecified_are_not_the_top_bar():
    from triage import dashboard as dash
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    day = "2026-11-04"
    rows = []
    samples = [
        ("AUKUS", "I will bomb the AUKUS program tomorrow."),
        ("AUKUS program", "We will attack the AUKUS program at the shipyard tomorrow."),
        ("Australian government", "I will bomb the Australian government office tomorrow."),
        ("Australian Labor government", "We will attack the Australian Labor government tomorrow."),
        ("Albanese government", "I will bomb the Albanese government offices tomorrow."),
        ("Labor Party", "We will attack the Labor Party headquarters tomorrow."),
        ("Australian Labor Party", "I will bomb the Australian Labor Party office tomorrow."),
        ("US bases", "We will attack the US bases in the north tomorrow."),
    ]
    for i, (target, text) in enumerate(samples):
        rows.append({
            "Document ID": f"t{i}", "Date": day, "URL": f"https://x.com/u/status/{i}",
            "Author Handle": f"@u{i}", "category": "threat", "is_flagged": True,
            "is_genuine": True, "threat_type": "violence", "targets": target,
            "Hit Sentence": text, "Engagement": 1,
        })
    for i in range(4):
        rows.append({
            "Document ID": f"u{i}", "Date": day, "URL": f"https://x.com/z/status/{i}",
            "Author Handle": f"@z{i}", "category": "threat", "is_flagged": True,
            "is_genuine": True, "threat_type": "violence", "targets": "Unspecified target",
            "Hit Sentence": f"Noted in passing number {i} without a named object.", "Engagement": 1,
        })
    # Same post stored twice under two labels must count once.
    rows.append({
        "Document ID": "dup", "Date": day, "URL": "https://x.com/u/status/0",
        "Author Handle": "@u0", "category": "threat", "is_flagged": True, "is_genuine": True,
        "threat_type": "call to protest", "targets": "AUKUS program",
        "Hit Sentence": samples[0][1], "Engagement": 1,
    })
    ctx = dash.build(pd.DataFrame(rows), now=now)
    labels = [row["label"] for row in ctx["threat_chart"]["rows"]]
    assert labels.count("AUKUS") == 1
    assert "AUKUS program" not in labels
    assert labels.count("Australian government") == 1
    assert "Labor Party" not in labels and "Albanese government" not in labels
    assert "US bases" in labels
    assert "Unspecified target" not in labels
    assert ctx["threat_chart"]["unspecified"] == 4
    assert ctx["threat_chart"]["rows"][0]["label"] != "Unspecified target"


def test_dashboard_hides_false_flags_and_duplicate_promo(monkeypatch):
    monkeypatch.setattr("triage.geo.persistence_enabled", lambda: False)
    monkeypatch.setattr("triage.geo._nominatim", lambda place: None)
    from triage import dashboard as dash
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    day = "2026-11-05"
    rows = [
        {"Document ID": "s1", "Date": day, "URL": "https://x.com/a/status/1",
         "Author Handle": "Unknown", "category": "threat", "threat_type": "none",
         "is_genuine": True, "Hit Sentence": SARCASM, "Engagement": 3},
        {"Document ID": "s2", "Date": day, "URL": "https://x.com/a/status/2",
         "Author Handle": "@a", "category": "threat", "threat_type": "violence",
         "is_genuine": True, "Hit Sentence": SPEND, "Opening Text": SPEND, "Engagement": 2},
        {"Document ID": "g1", "Date": day, "URL": "https://x.com/g/status/1",
         "Author Handle": "@greens", "category": "protest", "threat_type": "call to protest",
         "Hit Sentence": GREENS, "Engagement": 1},
        {"Document ID": "y1", "Date": day, "URL": "https://www.youtube.com/@BestFreeToursSydney/videos",
         "Author Name": "Unknown", "category": "threat", "threat_type": "call to protest",
         "is_genuine": True, "Hit Sentence": DIGEST, "Opening Text": DIGEST, "Engagement": 4},
        {"Document ID": "y2", "Date": day, "URL": "https://www.youtube.com/@BestFreeToursSydney/videos",
         "Author Name": "BestFreeToursSydney", "category": "threat", "threat_type": "call to protest",
         "is_genuine": True, "Hit Sentence": DIGEST, "Engagement": 4},
        {"Document ID": "e1", "Date": day, "link": "Supreme Court Gardens",
         "Author Handle": "@rally", "category": "protest", "Hit Sentence": SATURDAY, "Engagement": 1},
        {"Document ID": "e2", "Date": day, "URL": "https://bsky.app/profile/perth.bsky.social/post/abc",
         "Author Handle": "", "category": "protest", "Hit Sentence": SATURDAY, "Engagement": 1},
        {"Document ID": "real", "Date": day, "URL": "https://x.com/actualuser/status/9",
         "Author Handle": "Unknown", "Author Name": "Unknown", "category": "threat",
         "threat_type": "none", "Hit Sentence": DIRECT, "Opening Text": DIRECT, "Engagement": 9},
        {"Document ID": "yt", "Date": day, "URL": "https://www.youtube.com/watch?v=abc123",
         "Author Name": "Unknown", "category": "threat", "threat_type": "none",
         "Hit Sentence": "We will attack HMAS Stirling next week.", "Engagement": 1},
    ]
    ctx = dash.build(pd.DataFrame(rows), now=now)
    blob = " ".join(p["text"] for p in ctx["flagged_sample"])
    assert SARCASM not in blob and SPEND not in blob and DIGEST not in blob
    assert blob.count("I will bomb the AUKUS submarine base") == 1
    shown = {p["doc_id"]: p for p in ctx["flagged_sample"]}
    assert shown["real"]["platform"] == "X"
    assert shown["real"]["handle"] == "@actualuser"
    assert shown["real"]["type"] == ""
    assert shown["real"]["when"]
    assert shown["yt"]["platform"] == "YouTube"
    assert shown["yt"]["handle"] == ""
    assert "Unknown" not in shown["yt"]["platform"] and "Unknown" not in (shown["yt"]["handle"] or "x")
    assert ctx["posters"] == []
    places = [e["place"] for e in ctx["upcoming"]]
    assert "Supreme Court Gardens" in places
    blank = [e for e in ctx["upcoming"] if e["place"] == "Supreme Court Gardens" and not e["url"]]
    linked = [e for e in ctx["upcoming"] if e["url"].startswith("https://bsky.app/")]
    assert blank and linked
    assert all(e["url"] != "Supreme Court Gardens" for e in ctx["upcoming"])


def test_reupload_is_idempotent_and_cross_posts_stay(tmp_path):
    from triage.storage import Storage
    st = Storage(f"sqlite:///{tmp_path/'d.db'}")
    text = DIRECT
    first = pd.DataFrame([
        {"Document ID": "a", "Date": "2026-11-01", "URL": "https://x.com/a/status/9",
         "Author Handle": "@a", "Hit Sentence": text, "category": "threat", "is_flagged": True},
        {"Document ID": "b", "Date": "2026-11-01", "URL": "https://x.com/a/status/9",
         "Author Handle": "@a", "Hit Sentence": text + " " + text, "category": "threat", "is_flagged": True},
    ])
    st.save_run(first, ["a.xlsx"], 2)
    assert len(st.load_posts()) == 1
    st.save_run(first.iloc[:1], ["a.xlsx"], 1)
    assert len(st.load_posts()) == 1
    cross = pd.DataFrame([
        {"Document ID": "c", "Date": "2026-11-01", "URL": "https://bsky.app/profile/a.bsky.social/post/1",
         "Author Handle": "@a", "Hit Sentence": text, "category": "threat", "is_flagged": True},
    ])
    st.save_run(cross, ["c.xlsx"], 1)
    assert len(st.load_posts()) == 2


def test_repair_is_free_idempotent_and_keeps_rows_and_labels(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'p.db'}")
    monkeypatch.setenv("BACKFILL_APPROVED", "yes-i-approve-paid-backfill")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    def boom(*_a, **_k):
        raise AssertionError("paid model was called")
    monkeypatch.setattr("triage.refine.anthropic_complete", boom)
    from triage import feedback as fb
    from triage.repair import apply_repair, paid_reanalysis_report, plan_repair
    from triage.storage import Storage, rows_to_store
    fb._engine = None
    st = Storage()
    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    df = pd.DataFrame([
        {"Document ID": "keep-sarcasm", "Date": day, "URL": "https://x.com/a/status/3",
         "Hit Sentence": SARCASM, "category": "threat", "threat_type": "violence",
         "is_genuine": True, "is_flagged": True, "Engagement": 1},
        {"Document ID": "keep-advocacy", "Date": day, "URL": "https://x.com/a/status/4",
         "Hit Sentence": "Please make a submission on AUKUS.", "category": "protest",
         "mobilization_status": "Advocacy: inquiry/submission campaign",
         "is_flagged": True, "Engagement": 1},
        {"Document ID": "keep-real", "Date": day, "URL": "https://x.com/a/status/5",
         "Hit Sentence": DIRECT, "category": "threat", "threat_type": "violence",
         "is_flagged": True, "Engagement": 1},
    ])
    st.save_run(rows_to_store(df, df), ["s.xlsx"], 3)
    fb.set_label("post", "keep-sarcasm", "not_relevant", source="X")
    before = st.load_posts()
    quote = paid_reanalysis_report(10000)
    assert quote["over_limit"] and quote["usd"] > 5 and quote["ran"] is False and quote["will_run"] is False
    dry = plan_repair(st)
    assert dry["writes"] is False and dry["paid_model_called"] is False and dry["rows_deleted"] == 0
    assert dry["relevance_labels_touched"] == 0
    assert dry["paid_reanalysis"]["ran"] is False
    assert dry["posts_would_update"] >= 1
    applied = apply_repair(st)
    assert applied["paid_model_called"] is False and applied["rows_deleted"] == 0
    again = apply_repair(st)
    assert again["posts_updated"] == 0
    after = st.load_posts().set_index("Document ID")
    assert set(after.index) == set(before["Document ID"])
    assert SARCASM in str(after.loc["keep-sarcasm", "Hit Sentence"])
    assert (after.loc["keep-sarcasm", "category"] or "") not in {"threat", "protest", "both"}
    assert (after.loc["keep-advocacy", "category"] or "") not in {"threat", "protest", "both"}
    assert after.loc["keep-real", "category"] == "threat"
    assert fb.all_labels()[("post", "keep-sarcasm")] == "not_relevant"


def test_pipeline_does_not_flag_the_reported_examples(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PERSIST_RUNS", raising=False)
    from triage.pipeline import process
    calls = []

    def fake(system, user):
        calls.append(1)
        return '{"results": []}'

    import io
    buf = io.BytesIO()
    pd.DataFrame([
        {"Document ID": "1", "Date": "2026-11-01", "URL": "https://x.com/a/status/11",
         "Hit Sentence": SARCASM, "Author Handle": "@a"},
        {"Document ID": "2", "Date": "2026-11-01", "URL": "https://x.com/a/status/12",
         "Hit Sentence": SPEND, "Opening Text": SPEND, "Author Handle": "@a"},
        {"Document ID": "3", "Date": "2026-11-01", "URL": "https://www.youtube.com/@BestFreeToursSydney/videos",
         "Hit Sentence": DIGEST, "Author Name": "BestFreeToursSydney"},
        {"Document ID": "4", "Date": "2026-11-01", "URL": "https://bsky.app/profile/perth.bsky.social/post/1",
         "Hit Sentence": SATURDAY, "Author Handle": "@rally"},
        {"Document ID": "5", "Date": "2026-11-01", "URL": "https://x.com/a/status/15",
         "Hit Sentence": DIRECT, "Author Handle": "@a"},
    ]).to_excel(buf, index=False)
    res = process([( "in.xlsx", buf.getvalue())], refine_fn=fake, heat_fn=fake, storage=None)
    texts = " ".join(res.flagged.get("Hit Sentence", pd.Series(dtype=str)).astype(str))
    assert SARCASM not in texts and "schoolgirls" not in texts and "BestFreeToursSydney weekly" not in texts
    assert any(SATURDAY in str(v) for v in res.flagged.get("Hit Sentence", []))
    assert any(DIRECT in str(v) for v in res.flagged.get("Hit Sentence", []))
    assert res.posters.empty
