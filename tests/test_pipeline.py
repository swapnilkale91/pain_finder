"""End-to-end pipeline tests using fixture data — no network, no API key."""

import sqlite3

import pytest

from painfinder import db as dbm
from painfinder import extract as extract_mod
from painfinder import score as score_mod
from painfinder import usage as usage_mod
from painfinder.ingest.app_reviews import parse_review_entries
from painfinder.ingest.hn_jobs import extract_location, parse_comment_hits, strip_html

# --- Fixtures mirroring real API response shapes -----------------------------

ALGOLIA_COMMENT_HITS = [
    {
        "objectID": "1001",
        "author": "acme_hr",
        "created_at": "2026-07-01T15:00:00Z",
        "parent_id": 999,
        "comment_text": (
            "<p>Acme Corp | Ops Analyst | Remote (US) | Full-time</p>"
            "<p>We&#x27;re hiring an analyst to manually reconcile billing data between "
            "Stripe and NetSuite every month. You&#x27;ll also maintain our giant "
            "spreadsheet of customer contracts and keep Salesforce in sync with it.</p>"
        ),
    },
    {
        "objectID": "1002",
        "author": "short_post",
        "created_at": "2026-07-01T15:05:00Z",
        "parent_id": 999,
        "comment_text": "<p>Startup | Engineer | SF</p>",  # too short — dropped
    },
    {
        "objectID": "1003",
        "author": "no_text",
        "parent_id": 999,
        "comment_text": None,  # deleted comment — dropped
    },
]

ITUNES_REVIEW_ENTRIES = [
    {   # feed metadata entry (the app itself) — must be skipped
        "id": {"label": "https://apps.apple.com/us/app/notes/id123"},
        "title": {"label": "NotesApp"},
    },
    {
        "id": {"label": "review-501"},
        "im:rating": {"label": "1"},
        "title": {"label": "Lost everything"},
        "content": {"label": "The last update deleted every note I had. Sync fails constantly and support never responded."},
        "author": {"name": {"label": "angry_user"}},
        "updated": {"label": "2026-06-28T10:00:00-07:00"},
    },
    {
        "id": {"label": "review-502"},
        "im:rating": {"label": "5"},
        "title": {"label": "Love it"},
        "content": {"label": "Perfect app, no complaints."},
        "author": {"name": {"label": "happy_user"}},
        "updated": {"label": "2026-06-27T10:00:00-07:00"},
    },
]


# --- Parsing ------------------------------------------------------------------

def test_strip_html():
    assert strip_html("<p>Hello &amp; welcome</p><p>Line 2</p>") == "Hello & welcome\nLine 2"


def test_extract_location():
    assert extract_location("Acme | Ops Analyst | Remote (US) | Full-time") == "Remote (US)"
    assert extract_location("Acme | Engineer | Berlin, Germany") == "Berlin, Germany"
    assert extract_location("Acme | ONSITE in NYC | Senior Dev") == "ONSITE in NYC"
    assert extract_location("Acme hiring engineers") is None


def test_parse_hn_comments():
    items = parse_comment_hits(ALGOLIA_COMMENT_HITS, "Ask HN: Who is hiring? (July 2026)")
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "hn_jobs"
    assert item["external_id"] == "1001"
    assert item["title"].startswith("Acme Corp | Ops Analyst")
    assert "reconcile billing data" in item["text"]
    assert item["url"] == "https://news.ycombinator.com/item?id=1001"
    assert item["location"] == "Remote (US)"


def test_parse_review_entries():
    items = parse_review_entries(ITUNES_REVIEW_ENTRIES, "NotesApp")
    assert len(items) == 2
    assert items[0]["rating"] == 1
    assert items[0]["source"] == "app_reviews"
    assert "deleted every note" in items[0]["text"]
    # rating filter behavior lives in ingest(); emulate it here
    critical = [i for i in items if i["rating"] <= 3]
    assert len(critical) == 1


# --- Heuristic extraction -----------------------------------------------------

def test_heuristic_extraction_job_post():
    items = parse_comment_hits(ALGOLIA_COMMENT_HITS, "thread")
    pains = extract_mod.extract(items[0]["source"], items[0]["title"], items[0]["text"],
                                heuristic=True)
    categories = {p["category"] for p in pains}
    assert "manual_process" in categories
    assert "tooling_gap" in categories
    assert all(1 <= p["severity"] <= 5 for p in pains)


def test_heuristic_extraction_review():
    items = parse_review_entries(ITUNES_REVIEW_ENTRIES, "NotesApp")
    pains = extract_mod.extract("app_reviews", "NotesApp", items[0]["text"], heuristic=True)
    categories = {p["category"] for p in pains}
    assert "reliability" in categories  # "deleted every note"
    # the 5-star review should produce nothing
    happy = extract_mod.extract("app_reviews", "NotesApp", items[1]["text"], heuristic=True)
    assert happy == []


# --- DB + scoring end-to-end --------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    return dbm.connect(str(tmp_path / "test.db"))


def test_full_pipeline_heuristic(conn):
    job_items = parse_comment_hits(ALGOLIA_COMMENT_HITS, "thread")
    review_items = parse_review_entries(ITUNES_REVIEW_ENTRIES, "NotesApp")
    for i in review_items:
        i["domain"] = "note-taking"

    assert dbm.insert_raw_items(conn, job_items + review_items) == 3
    # idempotent re-ingest
    assert dbm.insert_raw_items(conn, job_items) == 0

    for item in dbm.unextracted_items(conn):
        pains = extract_mod.extract(item["source"], item["title"], item["text"],
                                    heuristic=True)
        dbm.save_pains(conn, item["id"], pains)

    stats = dbm.stats(conn)
    assert stats["unextracted"] == 0
    assert stats["pains"] > 0

    pains = [dict(r) for r in dbm.all_pains(conn)]
    # domain and location flow from ingest through to the extracted pains
    assert any(p["domain"] == "note-taking" for p in pains)
    assert any(p["location"] == "Remote (US)" for p in pains)

    themes = score_mod.build_themes(pains, heuristic=True)
    assert themes
    assert themes == sorted(themes, key=lambda t: -t["score"])
    # heuristic clustering rolls up the majority member domain
    review_theme = next(t for t in themes if t["name"] == "Reliability")
    assert review_theme["domain"] == "note-taking"
    dbm.replace_themes(conn, themes)
    assert dbm.stats(conn)["themes"] == len(themes)
    stored = conn.execute("SELECT domain FROM themes WHERE name = 'Reliability'").fetchone()
    assert stored["domain"] == "note-taking"


def test_batch_discount_and_haiku_prices(conn):
    tokens = {"input_tokens": 1000, "output_tokens": 200}
    full = usage_mod.cost_usd("claude-haiku-4-5", tokens)
    assert full == pytest.approx((1000 * 1 + 200 * 5) / 1e6)
    assert usage_mod.cost_usd("claude-haiku-4-5", tokens, batch=True) == pytest.approx(full / 2)
    assert usage_mod.record(conn, "extract", "claude-haiku-4-5", tokens,
                            batch=True) == pytest.approx(full / 2)


def test_normalize_pains_defensive():
    data = {"pains": [
        {"description": "Real pain", "category": "pricing", "severity": 9,
         "tools_mentioned": ["Stripe"], "quote": "q"},
        {"description": "", "category": "pricing", "severity": 3},        # dropped: no description
        {"description": "Odd cat", "category": "not_a_category", "severity": "x"},
    ]}
    pains = extract_mod._normalize_pains(data)
    assert len(pains) == 2
    assert pains[0]["severity"] == 5          # clamped from 9
    assert pains[1]["category"] == "other"    # unknown category coerced
    assert pains[1]["severity"] == 1          # unparseable severity floored


def test_usage_recording_and_summary(conn):
    tokens = {"input_tokens": 800, "output_tokens": 200,
              "cache_write_tokens": 600, "cache_read_tokens": 0}
    cost = usage_mod.record(conn, "extract", "claude-opus-4-8", tokens)
    # 800*$5 + 200*$25 + 600*$6.25 per MTok
    assert cost == pytest.approx((800 * 5 + 200 * 25 + 600 * 6.25) / 1e6)

    usage_mod.record(conn, "cluster", "claude-opus-4-8",
                     {"input_tokens": 1000, "output_tokens": 500})
    s = usage_mod.summary(conn)
    assert s["total"]["calls"] == 2
    assert s["total"]["cost_usd"] == pytest.approx(cost + (1000 * 5 + 500 * 25) / 1e6)
    assert {st["stage"] for st in s["stages"]} == {"extract", "cluster"}
    # input total includes cache tokens
    assert s["total"]["input_tokens"] == 800 + 600 + 1000


def test_score_rewards_corroboration_and_severity():
    base = {"id": 1, "source": "hn_jobs", "category": "x", "severity": 3, "description": "d"}
    one_source = score_mod.score_theme([base, {**base, "id": 2}])
    two_sources = score_mod.score_theme([base, {**base, "id": 2, "source": "app_reviews"}])
    assert two_sources["score"] > one_source["score"]

    mild = score_mod.score_theme([{**base, "severity": 1}])
    severe = score_mod.score_theme([{**base, "severity": 5}])
    assert severe["score"] > mild["score"]
