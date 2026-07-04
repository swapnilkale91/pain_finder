"""End-to-end pipeline tests using fixture data — no network, no API key."""

import sqlite3

import pytest

from painfinder import db as dbm
from painfinder import extract as extract_mod
from painfinder import score as score_mod
from painfinder import usage as usage_mod
from painfinder.ingest.app_reviews import parse_review_entries
from painfinder.ingest.github_issues import (
    build_repository_query,
    fetch_issues,
    normalize_repo,
    parse_issues,
    search_market_issues,
)
from painfinder.ingest.hn_discussions import (
    discover_stories,
    parse_discussion_hits,
    search_discussion_hits,
)
from painfinder.ingest.hn_jobs import extract_location, parse_comment_hits, strip_html
from painfinder.sources import get_source, source_family

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

ALGOLIA_DISCUSSION_HITS = [
    {
        "objectID": "2001",
        "author": "bookkeeper42",
        "created_at": "2026-06-20T12:00:00Z",
        "story_title": "Ask HN: Tools for small business accounting?",
        "comment_text": (
            "<p>We still manually copy every invoice into a spreadsheet because our bank "
            "doesn&#x27;t integrate with the accounting tool. We built our own import script "
            "as a workaround, but it breaks whenever the CSV format changes.</p>"
        ),
    },
    {
        "objectID": "2002",
        "author": "founder",
        "created_at": "2026-06-19T12:00:00Z",
        "story_title": "Ask HN: Who is hiring? (June 2026)",
        "comment_text": "<p>Company | Engineer | Remote. We are hiring for our platform.</p>",
    },
    {
        "objectID": "2003",
        "author": "brief",
        "created_at": "2026-06-18T12:00:00Z",
        "story_title": "Accounting software",
        "comment_text": "<p>Try a spreadsheet.</p>",
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

GITHUB_ISSUES = [
    {
        "number": 42,
        "title": "Sync fails after reconnecting",
        "body": "Our team has to manually re-import every record as a workaround.",
        "user": {"login": "octocat"},
        "labels": [{"name": "bug"}, {"name": "sync"}],
        "html_url": "https://github.com/acme/widget/issues/42",
        "created_at": "2026-07-01T12:00:00Z",
    },
    {
        "number": 43,
        "title": "Update dependency",
        "body": "Routine maintenance pull request.",
        "user": {"login": "bot"},
        "labels": [],
        "html_url": "https://github.com/acme/widget/pull/43",
        "created_at": "2026-07-01T13:00:00Z",
        "pull_request": {"url": "https://api.github.com/repos/acme/widget/pulls/43"},
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


def test_parse_hn_discussions_keeps_them_separate_from_job_posts():
    items = parse_discussion_hits(ALGOLIA_DISCUSSION_HITS, domain="bookkeeping")
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "hn"
    assert item["external_id"] == "2001"
    assert item["domain"] == "bookkeeping"
    assert "manually copy every invoice" in item["text"]
    assert item["url"] == "https://news.ycombinator.com/item?id=2001"


def test_hn_discussion_search_discovers_relevant_stories_then_comments():
    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            assert url.endswith("/search")
            self.calls.append(params)
            if params["tags"] == "story":
                return Response({"hits": [{
                    "objectID": "3001",
                    "title": "Ask HN: Tools for small business accounting?",
                }]})
            comment = dict(ALGOLIA_DISCUSSION_HITS[0])
            comment.pop("story_title")
            return Response({"hits": [comment], "nbPages": 1})

    session = Session()
    hits = search_discussion_hits(
        session, "bookkeeping", max_comments=10, max_stories=5,
        days=30, now_ts=2_000_000_000,
    )
    assert len(hits) == 1
    assert session.calls[0]["tags"] == "story"
    assert session.calls[0]["restrictSearchableAttributes"] == "title"
    assert session.calls[0]["numericFilters"] == (
        f"created_at_i>{2_000_000_000 - 30 * 86400}"
    )
    assert session.calls[1]["tags"] == "comment,story_3001"
    assert hits[0]["story_title"] == "Ask HN: Tools for small business accounting?"


def test_hn_story_discovery_excludes_job_threads():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": [
                {"objectID": "1", "title": "Ask HN: Who is hiring? (July 2026)"},
                {"objectID": "2", "title": "Ask HN: Better bookkeeping tools?"},
            ]}

    class Session:
        def get(self, url, params, timeout):
            return Response()

    stories = discover_stories(Session(), "bookkeeping", now_ts=2_000_000_000)
    assert [story["objectID"] for story in stories] == ["2"]


def test_parse_review_entries():
    items = parse_review_entries(ITUNES_REVIEW_ENTRIES, "NotesApp")
    assert len(items) == 2
    assert items[0]["rating"] == 1
    assert items[0]["source"] == "app_reviews"
    assert "deleted every note" in items[0]["text"]
    # rating filter behavior lives in ingest(); emulate it here
    critical = [i for i in items if i["rating"] <= 3]
    assert len(critical) == 1


def test_parse_github_issues_excludes_pull_requests():
    items = parse_issues(GITHUB_ISSUES, "acme/widget", domain="developer tools")
    assert len(items) == 1
    item = items[0]
    assert item["source"] == "github_issues"
    assert item["external_id"] == "acme/widget#42"
    assert item["author"] == "octocat"
    assert item["domain"] == "developer tools"
    assert "Labels: bug, sync" in item["text"]


def test_github_repo_validation():
    assert normalize_repo("acme/widget.git") == "acme/widget"
    with pytest.raises(ValueError):
        normalize_repo("widget")


def test_fetch_github_issues_filters_pull_requests_and_configures_pagination():
    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return Response(GITHUB_ISSUES if params["page"] == 1 else [])

    session = Session()
    issues = fetch_issues(session, "acme/widget", max_issues=10)
    assert [issue["number"] for issue in issues] == [42]
    assert session.calls[0][1]["per_page"] == 100


def test_source_registry_and_prompt_context():
    source = get_source("github_issues")
    assert source.label == "GitHub issue"
    assert source_family("hn_jobs") == source_family("job_posts") == "job_posts"
    content = extract_mod._build_user_content(
        "github_issues", "acme/widget #42", "Sync fails and requires a workaround",
    )
    assert content.startswith("GITHUB ISSUE")
    assert "Source guidance:" in content


def test_github_market_search_discovers_repositories_then_fetches_issues():
    assert build_repository_query("bookkeeping") == (
        '"bookkeeping" in:name,description,topics archived:false fork:false'
    )

    class Response:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params))
            if url.endswith("/search/repositories"):
                return Response({"items": [{
                    "full_name": "acme/widget", "has_issues": True,
                    "archived": False, "disabled": False,
                }]})
            assert url.endswith("/repos/acme/widget/issues")
            return Response(GITHUB_ISSUES)

    session = Session()
    issues = search_market_issues(
        session, "bookkeeping", state="open", max_issues=10, max_repos=5,
    )
    items = parse_issues(issues, domain="bookkeeping")
    assert session.calls[0][1]["q"] == (
        '"bookkeeping" in:name,description,topics archived:false fork:false'
    )
    assert session.calls[1][1]["state"] == "open"
    assert items[0]["external_id"] == "acme/widget#42"
    assert items[0]["domain"] == "bookkeeping"


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


def test_heuristic_extraction_github_issue():
    items = parse_issues(GITHUB_ISSUES, "acme/widget")
    pains = extract_mod.extract(
        items[0]["source"], items[0]["title"], items[0]["text"], heuristic=True,
    )
    categories = {p["category"] for p in pains}
    assert "reliability" in categories
    assert "manual_process" in categories


def test_heuristic_extraction_hn_discussion():
    item = parse_discussion_hits(ALGOLIA_DISCUSSION_HITS)[0]
    pains = extract_mod.extract(
        item["source"], item["title"], item["text"], heuristic=True,
    )
    categories = {p["category"] for p in pains}
    assert "manual_process" in categories
    assert "tooling_gap" in categories
    assert "data_integration" in categories


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


def test_cluster_index_dedupe():
    clusters = [
        {"name": "A", "description": "", "domain": "x", "indices": [0, 1, 1, 2]},
        {"name": "B", "description": "", "domain": "x", "indices": [2, 3]},   # 2 already taken
        {"name": "C", "description": "", "domain": "x", "indices": [0, 1]},   # all taken -> dropped
    ]
    deduped = score_mod._dedupe_cluster_indices(clusters)
    assert [c["name"] for c in deduped] == ["A", "B"]
    assert deduped[0]["indices"] == [0, 1, 2]
    assert deduped[1]["indices"] == [3]


def test_score_rewards_corroboration_and_severity():
    base = {"id": 1, "source": "hn_jobs", "category": "x", "severity": 3, "description": "d"}
    one_source = score_mod.score_theme([base, {**base, "id": 2}])
    two_sources = score_mod.score_theme([base, {**base, "id": 2, "source": "app_reviews"}])
    assert two_sources["score"] > one_source["score"]

    mild = score_mod.score_theme([{**base, "severity": 1}])
    severe = score_mod.score_theme([{**base, "severity": 5}])
    assert severe["score"] > mild["score"]


def test_score_does_not_double_count_same_source_family():
    base = {"id": 1, "source": "hn_jobs", "category": "x", "severity": 3,
            "description": "d"}
    same_family = score_mod.score_theme([
        base, {**base, "id": 2, "source": "job_posts"},
    ])
    independent = score_mod.score_theme([
        base, {**base, "id": 2, "source": "github_issues"},
    ])
    assert same_family["source_count"] == 1
    assert independent["source_count"] == 2
    assert independent["score"] > same_family["score"]
