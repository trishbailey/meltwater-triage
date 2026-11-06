import pandas as pd

from triage import filters, rules
from triage.dashboard import build


def test_palestine_rule_marks_off_topic_posts_only():
    df = pd.DataFrame([
        {"item_type": "post", "item_id": "1", "Hit Sentence": "Join the next Palestine rally, November 15th, 3pm, as part of the national day of action against repression."},
        {"item_type": "post", "item_id": "2", "Hit Sentence": "Rally for Palestine and against AUKUS submarines at Town Hall Saturday"},
        {"item_type": "post", "item_id": "3", "Hit Sentence": "Stop AUKUS rally at Henderson Saturday"},
        {"item_type": "post", "item_id": "4", "Hit Sentence": "Palestine vigil tonight", "is_genuine": False},
    ])
    saved = []
    labels = rules.apply(df, {}, lambda items, reason: saved.append(([i["item_id"] for i in items], reason)))
    assert saved == [(["1"], "off_topic")] and labels == {("post", "1"): "not_relevant"}
    assert rules.apply(df, {("post", "1"): "relevant"}, lambda *a: saved.append("again")) == {("post", "1"): "relevant"}
    assert "Palestine" in rules.prompt_text()


def test_petitions_are_excluded_unless_there_is_physical_protest_language():
    p = filters.Patterns()
    for text in ("Sign the petition for a Royal Commission into AUKUS now.", "ADD YOUR NAME to demand an AUKUS inquiry",
                 "Please sign our petition against the AUKUS submarines"):
        assert filters.is_petition_only(text)
        assert filters.screen_text(text, p)["category"] == ""
    for text in ("Sign the petition and join the rally at Town Hall Saturday 10am against AUKUS",
                 "Add your name, then march with us on Sunday to stop AUKUS"):
        assert not filters.is_petition_only(text)


def test_stored_petition_posts_leave_the_dashboard():
    df = pd.DataFrame([
        {"Document ID": "a", "item_type": "post", "item_id": "a", "category": "protest", "is_genuine": True,
         "refined_category": "protest", "Hit Sentence": "Sign the petition for a Royal Commission into AUKUS now.",
         "post_date": pd.Timestamp("2026-11-03"), "Date": "2026-11-03"},
        {"Document ID": "b", "item_type": "post", "item_id": "b", "category": "protest", "is_genuine": True,
         "refined_category": "protest", "Hit Sentence": "Rally at Town Hall Saturday to stop AUKUS",
         "post_date": pd.Timestamp("2026-11-03"), "Date": "2026-11-03"},
    ])
    assert build(df, {})["protests"]["count"] == 1


def test_analyze_button_endpoint_requires_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SECRET_KEY", "k" * 32)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "0")
    monkeypatch.setenv("APP_PASSWORD", "pw")
    from app import create_app
    c = create_app().test_client()
    assert c.get("/api/analyze").get_json() == {"running": False, "last": None}
    r = c.post("/api/analyze")
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.get_json()["error"]


def test_commentary_and_report_sharing_are_not_protests():
    from triage import filters as f
    p = f.Patterns()
    commentary = ("G'day #Auspol Watching the US #midterms? I am certain the Australian security establishment "
                  "will use this to normalise our relationship with the US and deflect concerns about US bases "
                  "and AUKUS. Those getting paychecks from AUKUS are chuffed.")
    report = ("RT @KellieTranter: The 'AUKUS Revolving Door'. See the AUKUS Public Inquiry report here - "
              "https://t.co/x #auspol #ENDAUKUS")
    for t in (commentary, report):
        assert f.screen_text(t, p)["category"] in ("", "threat")
    assert f.screen_text("Blockade the shipyard at Henderson on 14 November", p)["category"] == "protest"


def test_inquiry_report_summary_is_not_a_protest():
    from triage import filters as f
    t = ("AUKUS ENQUIRY REPORT RELEASED TODAY The independent public inquiry into AUKUS was created by the "
         "organizers and the Australian Peace and Security Forum, gathering public evidence and publishing findings.")
    assert f.screen_text(t, f.Patterns())["category"] == ""


def test_slogan_in_news_commentary_is_not_a_protest():
    from triage import filters as f
    p = f.Patterns()
    t = ("QT @AJEnglish: No to AUKUS https://t.co/x ; US commentator Tucker Carlson claims Israeli Prime Minister "
         "is pressuring President Donald Trump to launch a nuclear strike on Iran, targeting an enrichment facility.")
    assert f.screen_text(t, p)["category"] == ""
    assert f.screen_text("Rally against AUKUS: No to AUKUS, Saturday 14 November outside Town Hall", p)["category"] == "protest"


def test_figurative_and_off_topic_commentary_is_not_a_threat():
    from triage import filters as f
    p = f.Patterns()
    for t in ("@x The US midterms will drown out #auspol. Tomorrow's AUKUS Public Inquiry report at the National "
              "Press Club won't get a look-in.",
              "Rivka Lafair questions why Israel did not kill or expel all Palestinians in 1948. We will never "
              "forget ALP complicity #Auspol #AUKUS",
              "Kill the AUKUS deal now", "The AUKUS deal will hurt workers and damage the budget #AUKUS"):
        assert f.screen_text(t, p)["category"] == "", t
    assert f.screen_text("Someone should shoot the defence minister over AUKUS", p)["category"] == "threat"
    assert f.screen_text("I will blow up the AUKUS base", p)["category"] == "threat"


def test_forum_media_release_post_is_not_a_protest():
    from triage import filters as f
    t = ("This is not a protest. the 'AUKUS Forum' has now taken down its media release titled 'Start Building the "
         "East Coast Submarine Base Now' which nominated Newcastle as its preferred option. #auspol #AUKUS "
         "#AUKUSPublicInquiry #accountability #transparency ; The 'AUKUS Revolving Door")
    assert f.screen_text(t, f.Patterns())["category"] == ""


def test_map_falls_back_to_places_named_in_the_post():
    import pandas as pd
    from triage import mapdata
    df = pd.DataFrame([{"category": "protest", "document_id": "d1", "post_date": pd.Timestamp("2026-11-03"),
                        "Hit Sentence": "Rally against AUKUS this Saturday outside Town Hall in Perth", "link": "u"},
                       {"category": "protest", "document_id": "d2", "post_date": pd.Timestamp("2026-11-03"),
                        "Hit Sentence": "Join the march online everywhere", "link": "v"}])
    m = mapdata.build_map(df, days=None)
    assert [e["place"].lower() for e in m["events"]] == ["perth"]
    assert m["stats"] == {"protests": 2, "protests_located": 1, "from_text": 1}


def test_protest_targets_are_physical_sites_only():
    from triage.taxonomy import canon_site, site_targets, sites_from_text
    assert site_targets("AUKUS; Australian Government; HMAS Stirling; Navy") == ["HMAS Stirling (Rockingham, WA)"]
    assert site_targets("Henderson; Henderson shipyard; Sydney Town Hall") == [
        "Henderson shipyard (WA)", "Town hall / council"]
    assert canon_site("AUKUS") == "" and canon_site("the government") == "" and canon_site("Albanese") == ""
    assert canon_site("Fremantle Port") == "Fremantle Port"
    assert sites_from_text("Rally outside Parliament House against AUKUS") == ["Parliament House (Canberra)"]
    assert sites_from_text("Opposition to AUKUS in general") == []


def test_protest_section_targets_exclude_generic_names():
    import pandas as pd
    from triage import dashboard
    df = pd.DataFrame([{"category": "protest", "document_id": "p1", "Hit Sentence": "Rally at Henderson shipyard",
                        "post_date": pd.Timestamp("2026-11-03"), "refined_category": "protest", "is_genuine": True,
                        "targets": "AUKUS; Henderson shipyard", "narrative_summary": "x", "link": "u"}])
    ctx = dashboard.build(df, labels={("post", "p1"): "relevant"})
    labels = [t["label"] for t in ctx["protests"]["targets"]]
    assert labels == ["Henderson shipyard (WA)"]


def test_map_reloads_when_a_label_is_saved():
    import os
    root = os.path.join(os.path.dirname(__file__), "..", "templates")
    base = open(os.path.join(root, "_base.html")).read()
    dash = open(os.path.join(root, "dashboard.html")).read()
    assert base.count('"labels-saved"') == 2 and 'addEventListener("labels-saved"' in dash
