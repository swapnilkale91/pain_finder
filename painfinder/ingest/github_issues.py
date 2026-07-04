"""Ingest issues from public or token-accessible GitHub repositories."""

import math
import os

import requests

API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "pain_finder/0.2 (PMF research tool)"


def normalize_repo(repo: str) -> str:
    """Validate and normalize an ``owner/repo`` identifier."""
    repo = repo.strip().removesuffix(".git").strip("/")
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"GitHub repository must be 'owner/repo', got {repo!r}")
    return repo


def _issue_repo(issue: dict, fallback: str | None = None) -> str:
    """Resolve owner/repo from list or search API payloads."""
    if fallback:
        return normalize_repo(fallback)
    repository_url = (issue.get("repository_url") or "").rstrip("/")
    marker = "/repos/"
    if marker in repository_url:
        return normalize_repo(repository_url.split(marker, 1)[1])
    raise ValueError(f"GitHub issue {issue.get('number', '?')} has no repository identity")


def parse_issues(issues: list[dict], repo: str | None = None,
                 domain: str | None = None) -> list[dict]:
    """Map GitHub API issue objects to raw items, excluding pull requests."""
    items = []
    for issue in issues:
        if "pull_request" in issue:
            continue
        title = (issue.get("title") or "").strip()
        body = (issue.get("body") or "").strip()
        if not title or not body:
            continue
        labels = [
            label.get("name", "") if isinstance(label, dict) else str(label)
            for label in issue.get("labels", [])
        ]
        label_text = ", ".join(label for label in labels if label)
        text = f"{title}\n\n{body}"
        if label_text:
            text += f"\n\nLabels: {label_text}"
        number = issue["number"]
        issue_repo = _issue_repo(issue, fallback=repo)
        item = {
            "source": "github_issues",
            "external_id": f"{issue_repo}#{number}",
            "title": f"{issue_repo} #{number}: {title}"[:500],
            "author": (issue.get("user") or {}).get("login"),
            "text": text,
            "url": issue.get("html_url"),
            "posted_at": issue.get("created_at"),
        }
        if domain:
            item["domain"] = domain
        items.append(item)
    return items


def fetch_issues(session: requests.Session, repo: str, state: str = "all",
                 max_issues: int = 200, labels: str | None = None) -> list[dict]:
    """Fetch repository issues newest-first, following page numbers up to a cap."""
    if state not in {"open", "closed", "all"}:
        raise ValueError("state must be one of: open, closed, all")
    repo = normalize_repo(repo)
    issues = []
    page = 1
    while len(issues) < max_issues:
        # Keep page size stable so page-number pagination never skips records.
        per_page = 100
        params = {
            "state": state,
            "sort": "created",
            "direction": "desc",
            "per_page": per_page,
            "page": page,
        }
        if labels:
            params["labels"] = labels
        response = session.get(
            f"{API_ROOT}/repos/{repo}/issues", params=params, timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        issues.extend(issue for issue in batch if "pull_request" not in issue)
        if len(batch) < per_page:
            break
        page += 1
    return issues[:max_issues]


def build_repository_query(query: str) -> str:
    """Build a query for repositories whose purpose matches a market."""
    query = query.strip()
    if not query:
        raise ValueError("GitHub repository search query cannot be empty")
    return f'"{query}" in:name,description,topics archived:false fork:false'


def discover_repositories(session: requests.Session, query: str,
                          max_repos: int = 10) -> list[str]:
    """Find established, active repositories relevant to a market query."""
    if not 1 <= max_repos <= 100:
        raise ValueError("max_repos must be between 1 and 100")
    response = session.get(
        f"{API_ROOT}/search/repositories",
        params={
            "q": build_repository_query(query),
            "sort": "stars",
            "order": "desc",
            "per_page": max_repos,
            "page": 1,
        },
        timeout=30,
    )
    response.raise_for_status()
    return [
        repo["full_name"]
        for repo in response.json().get("items", [])
        if repo.get("full_name") and repo.get("has_issues", True)
        and not repo.get("archived") and not repo.get("disabled")
    ][:max_repos]


def search_market_issues(session: requests.Session, query: str, state: str = "all",
                         max_issues: int = 200, max_repos: int = 10) -> list[dict]:
    """Discover market-relevant repositories, then sample recent issues from each."""
    if max_issues < 1:
        raise ValueError("max_issues must be at least 1")
    repos = discover_repositories(session, query, max_repos=max_repos)
    if not repos:
        return []
    per_repo = max(1, math.ceil(max_issues / len(repos)))
    issues = []
    for repo in repos:
        repo_issues = fetch_issues(session, repo, state=state, max_issues=per_repo)
        for issue in repo_issues:
            issue.setdefault("repository_url", f"{API_ROOT}/repos/{repo}")
        issues.extend(repo_issues)
    return issues[:max_issues]


def ingest(repo: str | None = None, query: str | None = None,
           state: str = "all", max_issues: int = 200,
           max_repos: int = 10,
           labels: str | None = None, domain: str | None = None,
           token: str | None = None) -> tuple[str, list[dict]]:
    """Fetch repository issues or search market-wide by query.

    ``GITHUB_TOKEN`` is optional for public data but strongly recommended for search limits.
    """
    if bool(repo) == bool(query):
        raise ValueError("Provide exactly one of repo or query")
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    })
    token = token or os.environ.get("GITHUB_TOKEN")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    if repo:
        repo = normalize_repo(repo)
        issues = fetch_issues(
            session, repo, state=state, max_issues=max_issues, labels=labels,
        )
        return repo, parse_issues(issues, repo, domain=domain)

    issues = search_market_issues(
        session, query, state=state, max_issues=max_issues, max_repos=max_repos,
    )
    return query, parse_issues(issues, domain=domain or query)
