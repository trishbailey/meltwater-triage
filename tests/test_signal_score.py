
from triage.alerts import parse_alert, score_signal

def test_date_and_place_is_high():
    assert score_signal("This Saturday 7 Nov, Supreme Court Gardens") == "high"
    a = parse_alert({"source": "web", "url": "https://example.com/p", "signal_level": "low",
                     "text": "Meet This Saturday 7 Nov, Supreme Court Gardens", "matched_terms": []})
    assert a["signal_level"] == "high"

def test_does_not_downgrade_critical():
    assert score_signal("This Saturday at Hyde Park", suggested="critical") == "critical"

def test_no_place_stays_low():
    assert score_signal("Something happens this Saturday somewhere") == "low"
