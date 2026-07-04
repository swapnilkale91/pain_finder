"""Ingest Hacker News "Ask HN: Who is hiring?" threads via the Algolia HN API.

The monthly thread is posted by the `whoishiring` account; each top-level
comment is one job post. Both endpoints are free and unauthenticated:
  https://hn.algolia.com/api/v1/search_by_date  (find the thread)
  https://hn.algolia.com/api/v1/search          (fetch its comments)
"""

import html
import re

import requests

ALGOLIA = "https://hn.algolia.com/api/v1"
USER_AGENT = "pain_finder/0.1 (PMF research tool)"


_LOC_HINT = re.compile(r"\b(remote|onsite|on-site|hybrid|relocation)\b", re.I)


def extract_location(first_line: str) -> str | None:
    """Best-effort location from the conventional 'Company | Role | Location | ...'
    first line of an HN job post. Free — no LLM involved."""
    segments = [s.strip() for s in first_line.split("|")]
    hints = [s for s in segments if _LOC_HINT.search(s)]
    if hints:
        return "; ".join(hints[:2])[:120]
    if len(segments) >= 3 and segments[2]:
        return segments[2][:120]
    return None


def strip_html(text: str) -> str:
    """Convert HN comment HTML to plain text."""
    text = re.sub(r"<p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def parse_comment_hits(hits: list[dict], thread_title: str) -> list[dict]:
    """Map Algolia comment hits to raw_item dicts. Pure function for testability."""
    items = []
    for h in hits:
        text = h.get("comment_text") or ""
        if not text:
            continue
        plain = strip_html(text)
        if len(plain) < 80:  # too short to be a real job post
            continue
        # First line of an HN job post is conventionally "Company | Role | Location | ..."
        first_line = plain.split("\n", 1)[0][:200]
        items.append({
            "source": "hn_jobs",
            "external_id": str(h["objectID"]),
            "title": first_line,
            "author": h.get("author"),
            "text": plain,
            "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
            "posted_at": h.get("created_at"),
            "location": extract_location(first_line),
        })
    return items


def find_latest_thread(session: requests.Session) -> dict:
    """Return the most recent Who is Hiring story ({id, title})."""
    r = session.get(
        f"{ALGOLIA}/search_by_date",
        params={
            "query": '"Ask HN: Who is hiring?"',
            "tags": "story,author_whoishiring",
            "hitsPerPage": 5,
        },
        timeout=30,
    )
    r.raise_for_status()
    for hit in r.json()["hits"]:
        if hit.get("title", "").startswith("Ask HN: Who is hiring?"):
            return {"id": hit["objectID"], "title": hit["title"]}
    raise RuntimeError("Could not find a 'Who is hiring?' thread")


def fetch_thread_comments(session: requests.Session, story_id: str,
                          max_comments: int = 500) -> list[dict]:
    """Fetch top-level comments (job posts) for a story, paginated."""
    hits, page = [], 0
    while len(hits) < max_comments:
        r = session.get(
            f"{ALGOLIA}/search_by_date",
            params={"tags": f"comment,story_{story_id}", "hitsPerPage": 100, "page": page},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        batch = [h for h in data["hits"] if str(h.get("parent_id")) == str(story_id)]
        hits.extend(batch)
        page += 1
        if page >= data.get("nbPages", 1):
            break
    return hits[:max_comments]


def ingest(max_comments: int = 500) -> tuple[str, list[dict]]:
    """Fetch the latest thread's job posts. Returns (thread_title, raw_items)."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    thread = find_latest_thread(session)
    hits = fetch_thread_comments(session, thread["id"], max_comments)
    return thread["title"], parse_comment_hits(hits, thread["title"])
