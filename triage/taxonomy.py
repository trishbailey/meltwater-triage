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


def targets_from_text(text: str) -> list[str]:
    """Every target label whose rule matches the post text (used until Claude's analysis exists)."""
    low = (text or "").lower()
    return [label for label, pattern in _TARGET_RULES if re.search(pattern, low)]


# Protest targets must be physical places. Each rule names one site; generic labels such as "AUKUS" or
# "Australian Government" are not targets for the map-style analysis.
_SITE_RULES = [
    ("HMAS Stirling (Rockingham, WA)", r"stirling|rockingham|garden island,? (wa|western)"),
    ("Henderson shipyard (WA)", r"henderson"),
    ("Osborne shipyard (Adelaide)", r"osborne|\bedinburgh\b"),
    ("Pine Gap (Alice Springs)", r"pine gap|alice springs"),
    ("Parliament House (Canberra)", r"parliament house|\bparliament\b|capital hill|russell offices|canberra"),
    ("Garden Island (Sydney)", r"garden island|kuttabul|fleet base east"),
    ("Port Kembla (Wollongong)", r"port kembla|wollongong"),
    ("Newcastle", r"newcastle|port of newcastle"),
    ("RAAF Tindal / Darwin", r"tindal|darwin|katherine"),
    ("North West Cape (Exmouth)", r"north west cape|exmouth|harold holt"),
    ("US Embassy / Consulate", r"us (embassy|consulate)|american (embassy|consulate)|u\.s\. (embassy|consulate)|"
                               r"consulate"),
    ("Defence headquarters / offices", r"submarine agency|majura|defen[cs]e (hq|headquarters|offices?)|bae systems|"
                                      r"austal|lockheed.*(office|plant)"),
    ("Town hall / council", r"town hall|city hall|council chambers"),
    ("State parliament", r"nsw parliament|state parliament|parliament of (nsw|victoria|wa|south australia)"),
]
_GENERIC_TARGET = re.compile(
    r"^(aukus|the aukus (deal|pact|agreement|program|treaty)|(australian |federal |us |uk )?gov(ernment)?|"
    r"labor|alp|coalition|military|defen[cs]e|navy|police|nuclear|war|australia|the government|politicians?)$", re.I)
_PHYSICAL_WORD = re.compile(
    r"\b(base|shipyard|dockyard|yard|port|harbou?r|naval|hmas|raaf|facility|gates?|headquarters|hq|embassy|consulate|"
    r"house|hall|office|offices|centre|center|square|park|street|road|airport|airfield|station|camp|wharf|terminal|pier|depot|plant|warship|vessel|ship|submarine base)\b", re.I)


def canon_site(text: str) -> str:
    """The specific physical site a target names, or "" when it is generic ("AUKUS", "the government")."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text or _GENERIC_TARGET.match(text):
        return ""
    low = text.lower()
    for label, pattern in _SITE_RULES:
        if re.search(pattern, low):
            return label
    return text if _PHYSICAL_WORD.search(text) else ""


def site_targets(raw: str) -> list[str]:
    """Distinct physical sites in a semicolon-separated target field."""
    seen: dict[str, str] = {}
    for part in (raw or "").split(";"):
        label = canon_site(part)
        if label:
            seen.setdefault(label.lower(), label)
    return list(seen.values())


def sites_from_text(text: str) -> list[str]:
    """Sites named in the post text (used until Claude's analysis exists)."""
    low = (text or "").lower()
    return [label for label, pattern in _SITE_RULES if re.search(pattern, low)]
