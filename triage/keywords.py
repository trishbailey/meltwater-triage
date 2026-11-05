"""Editable keyword lists for threat and protest screening.

Edit the lists below. Matching is case-insensitive and whole-word/phrase;
longer phrases are tried first.
"""

PERSON_NAMES = [
]

THREAT_TERMS = [
    "kill",
    "murder",
    "assassinate",
    "shoot",
    "stab",
    "poison",
    "strangle",
    "bomb",
    "explode",
    "blow up",
    "attack",
    "beat up",
    "hurt",
    "harm",
    "torture",
    "kidnap",
    "abduct",
    "hostage",
    "take hostage",
    "execute",
    "slaughter",
    "lynch",
    "hang",
    "behead",
    "decapitate",
    "drown",
    "suffocate",
    "massacre",
    "maim",
    "cripple",
    "destroy",
    "burn down",
    "set fire to",
    "threat",
    "threaten",
    "warn you",
    "better watch",
    "pay for this",
    "know where you live",
    "track down",
    "hunt down",
    "wipe out",
    "sabotage",
    "sabotaged",
    "sabotaging",
    "vandalize",
    "damage",
    "cut cables",
    "cut the power",
    "disable",
    "traitor",
    "gallows",
    "guillotine",
    "firing squad",
]

AUKUS_TARGET_TERMS = [
    "AUKUS",
    "SSN-AUKUS",
    "Pillar One",
    "Pillar II",
    "nuclear submarine",
    "nuclear-powered submarine",
    "submarine program",
    "submarine base",
    "submarine facility",
    "submarine shipyard",
    "HMAS Stirling",
    "Garden Island",
    "Osborne Naval Shipyard",
    "Henderson shipyard",
    "ASC Pty",
    "Australian Submarine Agency",
    "Royal Australian Navy",
    "Australian Defence Force",
    "Australian defense force",
    "UK Ministry of Defence",
    "US Department of Defense",
]

PERSON_ROLE_TERMS = [
    "AUKUS official",
    "AUKUS officials",
    "AUKUS minister",
    "AUKUS ministers",
    "defence minister",
    "defense minister",
    "prime minister",
    "foreign minister",
    "naval officer",
    "navy officer",
    "naval personnel",
    "navy personnel",
    "submarine crew",
    "submarine crews",
    "defence personnel",
    "defense personnel",
    "shipyard worker",
    "shipyard workers",
    "politician",
    "politicians",
    "senator",
    "senators",
    "member of parliament",
]

ACTION_NOUNS = [
    "rally",
    "protest",
    "demo",
    "demonstration",
    "march",
    "blockade",
    "picket",
    "vigil",
    "sit-in",
    "die-in",
    "occupation",
    "encampment",
    "flotilla",
    "convoy",
    "peace camp",
    "community assembly",
    "teach-in",
    "speak-out",
    "forum",
    "public meeting",
    "day of action",
    "week of action",
    "national day of action",
]

CALL_TO_ACTION = [
    "join us",
    "come along",
    "bring a friend",
    "bring banners",
    "bring placards",
    "show up",
    "turn out",
    "all welcome",
    "meet at",
    "assemble at",
    "gather at",
    "RSVP",
    "register",
    "sign up",
    "spread the word",
    "share widely",
    "we need numbers",
    "stand with",
    "solidarity with",
    "mobilise",
    "mobilize",
]

DISRUPTION_TERMS = [
    "shut down",
    "shut it down",
    "blockade the gates",
    "stop the ship",
    "stop the sub",
    "stop the shipment",
    "picket line",
    "no to AUKUS",
    "AUKUS out",
    "nukes out",
    "close Pine Gap",
    "not in our name",
    "direct action",
    "civil disobedience",
    "lock-on",
    "arrestable",
    "non-arrestable",
    "legal observer",
    "know your rights",
    "police liaison",
    "peacekeepers",
]

ORGANIZER_TERMS = [
    "Stop AUKUS WA",
    "IPAN",
    "No AUKUS Coalition",
    "Anti-AUKUS Coalition",
    "Wollongong Against War and Nukes",
    "Nuclear Free WA",
    "MUA",
    "CFMEU",
    "endorsed by",
    "co-hosted by",
]

LOGISTICS_TERMS = [
    "outside",
    "meeting point",
    "carpool",
    "buses from",
    "transport available",
    "picket training",
    "marshal briefing",
    "training",
    "Parliament House",
    "HMAS Stirling",
    "Garden Island",
    "Henderson",
    "the gates",
]

HASHTAGS = [
    "#NoAUKUS",
    "#StopAUKUS",
    "#AUKUSout",
    "#NoNuclearSubs",
    "#NoUSBases",
    "#PeaceNotWar",
    "#Rockingham",
    "#Wollongong",
]

INQUIRY_TERMS = [
    "inquiry",
    "submission",
    "make a submission",
    "public consultation",
]

PAST_EVENT_TERMS = [
    "yesterday",
    "last week",
    "last month",
    "last saturday",
    "was held",
    "took place",
    "photos from",
    "recap",
    "attended the",
]

FUTURE_CUES = [
    "will be",
    "this saturday",
    "this sunday",
    "tomorrow",
    "join us",
    "come along",
    "register",
    "RSVP",
]

UNION_ACTION_TERMS = [
    "industrial action",
    "strike",
    "stop work",
    "work stoppage",
]

AUKUS_DEFENSE_CONTEXT = [
    "AUKUS",
    "submarine",
    "defence",
    "defense",
    "navy",
    "shipyard",
    "HMAS Stirling",
    "Garden Island",
    "Henderson",
    "Osborne",
    "nuclear-powered",
]


# Text columns screened (case-insensitive header match); all present ones are joined.
TEXT_COLUMN_OPTIONS = [
    "title", "opening text", "hit sentence", "post text", "text", "content",
    "message", "tweet", "full text", "comment",
]

# Columns tried, in order, for the output "link" column.
LINK_COLUMN_OPTIONS = ["url", "link", "permalink"]

# Engagement/reach columns coerced to numbers in the output (case-insensitive).
NUMERIC_COLUMNS = [
    "engagement", "shares", "quotes", "likes", "replies", "reposts", "comments",
    "reactions", "views", "estimated views", "reach", "global reach",
    "national reach", "local reach", "episode reach", "social echo",
    "editorial echo", "ave", "emv",
]

# Matched case-sensitively as whole words (e.g. "MP" but not "mp" or "Mp").
CASE_SENSITIVE_TARGET_TERMS = [
    "MP",
]

# Weak threat terms: count only when a target term appears within
# THREAT_PROXIMITY_WORDS words of the term (other threat terms just need a target anywhere).
CONTEXT_REQUIRED_THREAT_TERMS = [
    "hang",
    "attack",
    "destroy",
]
THREAT_PROXIMITY_WORDS = 8

# Weak protest terms: count only when the post has at least one other protest
# signal (action, call to action, disruption, organizer, or hashtag match).
WEAK_PROTEST_TERMS = [
    "demo",
    "register",
    "training",
    "outside",
]
