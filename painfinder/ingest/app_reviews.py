"""Ingest App Store customer reviews via Apple's public RSS feeds.

Free, unauthenticated endpoints:
  https://itunes.apple.com/search?term=...&entity=software     (find app IDs)
  https://itunes.apple.com/{cc}/rss/customerreviews/...        (fetch reviews)
"""

import requests

USER_AGENT = "pain_finder/0.1 (PMF research tool)"


def parse_review_entries(entries: list[dict], app_name: str) -> list[dict]:
    """Map RSS feed entries to raw_item dicts. Pure function for testability."""
    items = []
    for e in entries:
        # The first entry of the feed is sometimes the app itself, not a review.
        if "im:rating" not in e:
            continue
        review_id = e.get("id", {}).get("label")
        content = (e.get("content", {}) or {}).get("label", "")
        title = (e.get("title", {}) or {}).get("label", "")
        if not review_id or not content:
            continue
        text = f"{title}\n{content}".strip()
        items.append({
            "source": "app_reviews",
            "external_id": review_id,
            "title": app_name,
            "author": (e.get("author", {}).get("name", {}) or {}).get("label"),
            "rating": int(e["im:rating"]["label"]),
            "text": text,
            "url": None,
            "posted_at": (e.get("updated", {}) or {}).get("label"),
        })
    return items


def search_apps(session: requests.Session, term: str, country: str = "us",
                limit: int = 10) -> list[dict]:
    """Search the App Store; returns [{id, name}] ranked by store relevance."""
    r = session.get(
        "https://itunes.apple.com/search",
        params={"term": term, "entity": "software", "limit": limit, "country": country},
        timeout=30,
    )
    r.raise_for_status()
    return [
        {"id": res["trackId"], "name": res["trackName"]}
        for res in r.json().get("results", [])
        if "trackId" in res
    ]


def search_app(session: requests.Session, term: str, country: str = "us") -> dict:
    """Look up the single best-matching app by name; returns {id, name}."""
    apps = search_apps(session, term, country, limit=1)
    if not apps:
        raise RuntimeError(f"No App Store app found for {term!r}")
    return apps[0]


def fetch_reviews(session: requests.Session, app_id: int, country: str = "us",
                  pages: int = 5) -> list[dict]:
    """Fetch review feed entries (max 10 pages / ~500 reviews per app)."""
    entries = []
    for page in range(1, min(pages, 10) + 1):
        url = (f"https://itunes.apple.com/{country}/rss/customerreviews/"
               f"page={page}/id={app_id}/sortby=mostrecent/json")
        r = session.get(url, timeout=30)
        if r.status_code == 404:  # past the last page
            break
        r.raise_for_status()
        feed = r.json().get("feed", {})
        batch = feed.get("entry", [])
        if isinstance(batch, dict):  # single-entry pages come back as a dict
            batch = [batch]
        if not batch:
            break
        entries.extend(batch)
    return entries


def ingest(app_term: str | None = None, app_id: int | None = None,
           country: str = "us", pages: int = 5,
           max_rating: int | None = None,
           domain: str | None = None) -> tuple[str, list[dict]]:
    """Fetch reviews for one app (by search term or ID).

    max_rating filters to critical reviews (e.g. 3 = only 1-3 star reviews),
    which carry the pain signal.
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    if app_id is None:
        if not app_term:
            raise ValueError("Provide app_term or app_id")
        app = search_app(session, app_term, country)
        app_id, app_name = app["id"], app["name"]
    else:
        app_name = app_term or str(app_id)
    entries = fetch_reviews(session, app_id, country, pages)
    items = parse_review_entries(entries, app_name)
    if max_rating is not None:
        items = [i for i in items if i["rating"] <= max_rating]
    if domain:
        for i in items:
            i["domain"] = domain
    return app_name, items


def ingest_domain(domain: str, query: str | None = None, country: str = "us",
                  top: int = 10, pages: int = 3,
                  max_rating: int | None = 3) -> tuple[list[str], list[dict]]:
    """Sweep an entire domain: search the App Store for the domain (or a custom
    query), then ingest critical reviews for the top N matching apps, tagging
    every item with the domain label.

    Returns ([app names], raw_items).
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    apps = search_apps(session, query or domain, country, limit=top)
    if not apps:
        raise RuntimeError(f"No App Store apps found for {query or domain!r}")
    all_items, names = [], []
    for app in apps:
        entries = fetch_reviews(session, app["id"], country, pages)
        items = parse_review_entries(entries, app["name"])
        if max_rating is not None:
            items = [i for i in items if i["rating"] <= max_rating]
        for i in items:
            i["domain"] = domain
        all_items.extend(items)
        names.append(app["name"])
    return names, all_items
