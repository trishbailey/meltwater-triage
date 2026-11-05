"""Seerist remains an alert source; PDF upload UI is removed."""
from triage.alerts import SOURCES, parse_alert
from triage.seerist import item_key, parse_lines, to_alert

SAMPLE = """Search Result Report - Nov 05, 2026 SEERIST
Showing 50 of 701 results
Nov 05, 2026 · News & Social
1
Plus 2000 US AUKUS staff coming to Rockingham, housing...
Plus 2000 US AUKUS staff coming to Rockingham, housing supplied by our govt.
https://m.facebook.com/cndcymru/posts/
pfbid031msirNz1VN2
Countries: Australia · Sentiment: Negative · Author: CND Cymru
"""


def test_seerist_still_allowed_source():
    assert "seerist" in SOURCES
    a = parse_alert({"source": "seerist", "url": "https://example.com/a", "signal_level": "low",
                     "text": "x", "matched_terms": ["Location: Perth"]})
    assert a["source"] == "seerist"


def test_parse_lines_still_works():
    items = parse_lines(SAMPLE.splitlines())
    assert len(items) == 1
    assert items[0]["url"].startswith("https://")
    assert to_alert(items[0])["source"] == "seerist"
    assert item_key({"title": "t", "date": "Nov 05, 2026", "url": ""})
