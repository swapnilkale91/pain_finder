"""Ingest comments from Hacker News stories relevant to a market query."""

import math
import time

import requests

from .hn_jobs import ALGOLIA, USER_AGENT, strip_html


def _is_job_thread(title: str) -> bool:
    lowered = title.lower()
    return "who is hiring?" in lowered or "who wants to be hired?" in lowered


def parse_discussion_hits(hits: list[dict], domain: str | None = None) -> list[dict]:
    """Map Algolia comment results to HN discussion evidence."""
    items = []
    for hit in hits:
        story_title = (hit.get("story_title") or "Hacker News discussion").strip()
        if _is_job_thread(story_title):
            continue
        text = strip_html(hit.get("comment_text") or "")
        if len(text) < 80:
            continue
        object_id = hit.get("objectID")
        if not object_id:
            continue
        item = {
            "source": "hn",
            "external_id": str(object_id),
            "title": story_title[:500],
            "author": hit.get("author"),
            "text": text,
            "url": f"https://news.ycombinator.com/item?id={object_id}",
            "posted_at": hit.get("created_at"),
        }
        if domain:
            item["domain"] = domain
        items.append(item)
    return items


def discover_stories(session: requests.Session, query: str, max_stories: int = 10,
                     days: int = 730, now_ts: int | None = None) -> list[dict]:
    """Find recent stories whose titles match a market, ranked by relevance."""
    query = query.strip()
    if not query:
        raise ValueError("HN discussion query cannot be empty")
    if not 1 <= max_stories <= 100:
        raise ValueError("max_stories must be between 1 and 100")
    if days < 1:
        raise ValueError("days must be at least 1")

    since = (now_ts if now_ts is not None else int(time.time())) - days * 86400
    response = session.get(
        f"{ALGOLIA}/search",
        params={
            "query": query,
            "tags": "story",
            "numericFilters": f"created_at_i>{since}",
            "restrictSearchableAttributes": "title",
            "hitsPerPage": max_stories,
            "page": 0,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        story for story in response.json().get("hits", [])
        if story.get("objectID") and story.get("title")
        and not _is_job_thread(story["title"])
    ][:max_stories]


def fetch_story_comments(session: requests.Session, story_id: str,
                         max_comments: int) -> list[dict]:
    """Fetch comments from one story, ranked by relevance and engagement."""
    hits = []
    page = 0
    while len(hits) < max_comments:
        response = session.get(
            f"{ALGOLIA}/search",
            params={
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": 100,
                "page": page,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        batch = data.get("hits", [])
        if not batch:
            break
        hits.extend(batch)
        page += 1
        if page >= data.get("nbPages", 1):
            break
    return hits[:max_comments]


def search_discussion_hits(session: requests.Session, query: str,
                           max_comments: int = 100, max_stories: int = 10,
                           days: int = 730,
                           now_ts: int | None = None) -> list[dict]:
    """Discover relevant stories, then sample their comments."""
    if max_comments < 1:
        raise ValueError("max_comments must be at least 1")
    stories = discover_stories(
        session, query, max_stories=max_stories, days=days, now_ts=now_ts,
    )
    if not stories:
        return []
    per_story = max(1, math.ceil(max_comments / len(stories)))
    hits = []
    seen = set()
    for story in stories:
        for hit in fetch_story_comments(session, str(story["objectID"]), per_story):
            object_id = hit.get("objectID")
            if not object_id or object_id in seen:
                continue
            seen.add(object_id)
            hit.setdefault("story_title", story["title"])
            hit.setdefault("story_id", story["objectID"])
            hits.append(hit)
    return hits[:max_comments]


def ingest(query: str, domain: str | None = None, max_comments: int = 100,
           max_stories: int = 10, days: int = 730) -> tuple[str, list[dict]]:
    """Search recent HN comments and return normalized discussion evidence."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    hits = search_discussion_hits(
        session, query, max_comments=max_comments,
        max_stories=max_stories, days=days,
    )
    return query, parse_discussion_hits(hits, domain=domain or query)
