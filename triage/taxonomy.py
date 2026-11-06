"""Fixed groupings for the dashboard's topic and target analysis.

Claude's free-text labels ("AUKUS opposition", "Opposition to AUKUS", "AUKUS termination") collapse
into one label each here, so counts are not split across near-duplicates. The rules apply when the
dashboard is built, so posts reviewed before this module existed are grouped too. The raw labels stay
in the Excel downloads.
"""
from __future__ import annotations

import re

PROTEST_TOPICS = [
    "AUKUS opposition and termination",
    "Inquiry and transparency",
    "Nuclear safety and waste",
    "Sovereignty and US alliance",
    "Cost and jobs",
    "Military bases and facilities",
    "Peace and anti-war",
    "Indigenous and land rights",
    "Other AUKUS-related",
    "Other",
]

# First matching rule wins, so order runs from most to least specific.
_TOPIC_RULES = [
    ("Inquiry and transparency", r"inquir|transparen|submission|senate|parliament|disclos|scrutin|review|accountab"),
    ("Nuclear safety and waste", r"nuclear (safety|waste|risk|accident|reactor)|radioactiv|\bwaste\b|reactor|\bsafety\b|contaminat"),
    ("Sovereignty and US alliance", r"sovereign|us alliance|american|\bus\b|united states|china|independen|foreign policy"),
    ("Cost and jobs", r"\bcost|\bjobs?\b|spending|budget|taxpayer|price|economic|industry|employment"),
    ("Military bases and facilities", r"\bbase\b|bases|stirling|pine gap|osborne|henderson|shipyard|facilit|\bport\b"),
    ("Indigenous and land rights", r"indigenous|first nations|aboriginal|land rights|sacred|traditional owner|native title"),
    ("Peace and anti-war", r"\bpeace\b|anti-?war|disarm|militar|war\b"),
    ("AUKUS opposition and termination", r"oppos|terminat|cancel|scrap|end(ing)? aukus|stop|withdraw|reject|against|anti-?aukus|abolish|pull out|campaign"),
    ("Other AUKUS-related", r"aukus|submarine|pact|defen[cs]e (deal|program|alliance)"),
]

_TARGET_RULES = [
    ("AUKUS", r"aukus|submarine (pact|deal|program)|nuclear.powered submarine|defen[cs]e (deal|program|pact|alliance)|\bssn\b"),
    ("Australian Government", r"albanese|australian gov|federal gov|commonwealth|labor|marles|wong|prime minister|parliament|"
                              r"minister|coalition|dutton|department of defen|defen[cs]e department|\badf\b|australian defen|government"),
    ("US government and military", r"united states|\bus (gov|navy|military|forces|marines|president)|\busa\b|trump|biden|pentagon|american|washington"),
    ("UK government and industry", r"\buk\b|united kingdom|britain|british|\bbae\b|rolls.?royce|starmer"),
    ("Military bases and facilities", r"stirling|pine gap|osborne|henderson|\bbase\b|bases|shipyard|garden island|darwin|tindal|"
                                     r"north west cape|exmouth|facility|naval"),
    ("Defence contractors", r"lockheed|austal|\basc\b|raytheon|northrop|boeing|contractor|general dynamics|thales|defen[cs]e industry"),
    ("Police and security forces", r"police|\bafp\b|asio|security forces|military personnel|\bsoldiers?\b"),
]


def _match(text: str, rules) -> str | None:
    low = text.lower()
    for label, pattern in rules:
        if re.search(pattern, low):
            return label
    return None


def canon_topic(text: str, kind: str = "protest") -> str:
    """One of PROTEST_TOPICS for a free-text protest topic, or "" if blank."""
    text = (text or "").strip()
    if not text:
        return ""
    return _match(text, _TOPIC_RULES) or "Other"


def canon_target(text: str) -> str:
    """Group a target name; names no rule covers keep their own label."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ""
    return _match(text, _TARGET_RULES) or (text if text != text.lower() else text.capitalize())


def canon_targets(raw: str) -> list[str]:
    """Distinct grouped targets in a semicolon-separated field; one count per post."""
    seen: dict[str, str] = {}
    for part in (raw or "").split(";"):
        label = canon_target(part)
        if label:
            seen.setdefault(label.lower(), label)
    return list(seen.values())
