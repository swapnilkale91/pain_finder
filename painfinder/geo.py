"""Coarse geography normalization.

Locations arrive messy: App Store storefront codes ('US', 'IN'), free-text
job-post locations ('Remote (US)', 'Berlin, Germany | ONSITE'). This maps
them into a small set of buckets good enough for grouping — not geocoding.
"""

import re

_CODE_BUCKETS = {
    "US": "United States", "IN": "India", "GB": "United Kingdom", "UK": "United Kingdom",
    "DE": "Germany", "CA": "Canada", "AU": "Australia", "BR": "Brazil",
    "FR": "France", "NL": "Netherlands", "SG": "Singapore", "JP": "Japan",
}

_PATTERNS = [
    ("United States", r"\b(usa?|u\.s\.|united states|america|nyc|new york|san francisco|sf bay|austin|seattle|boston|chicago|los angeles|denver|portland)\b"),
    ("India", r"\b(india|bangalore|bengaluru|mumbai|delhi|gurgaon|hyderabad|pune|chennai|noida)\b"),
    ("United Kingdom", r"\b(uk|united kingdom|london|manchester|edinburgh)\b"),
    ("Germany", r"\b(germany|berlin|munich|münchen|hamburg|cologne)\b"),
    ("Canada", r"\b(canada|toronto|vancouver|montreal|ottawa)\b"),
    ("Europe (other)", r"\b(europe|amsterdam|paris|madrid|barcelona|lisbon|stockholm|copenhagen|dublin|zurich|zürich|vienna|warsaw|prague|tallinn|helsinki|oslo|brussels)\b"),
    ("Asia (other)", r"\b(singapore|tokyo|japan|seoul|korea|hong ?kong|taipei|jakarta|manila|vietnam|bangkok)\b"),
    ("Latin America", r"\b(brazil|brasil|mexico|argentina|colombia|chile|s[ãa]o paulo|buenos aires)\b"),
]

_REMOTE = re.compile(r"\bremote\b", re.I)


def coarse_location(location: str | None) -> str:
    """Map a raw location string to a coarse bucket.

    Country beats 'Remote' when both appear ('Remote (US)' -> United States),
    since the person is still *in* that geography.
    """
    if not location:
        return "Unknown"
    loc = location.strip()
    if loc.upper() in _CODE_BUCKETS:
        return _CODE_BUCKETS[loc.upper()]
    lowered = loc.lower()
    for bucket, pattern in _PATTERNS:
        if re.search(pattern, lowered):
            return bucket
    if _REMOTE.search(lowered):
        return "Remote (unspecified)"
    return "Other"
