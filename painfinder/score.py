"""Cluster extracted pains into themes and score them.

Clustering engines mirror extraction: Claude (semantic) or heuristic (by category).
Scoring is deterministic either way.
"""

import math
from collections import defaultdict

from pydantic import BaseModel, Field

MODEL = "claude-opus-4-8"


class Theme(BaseModel):
    name: str = Field(description="Short name for the pain theme, 3-8 words")
    description: str = Field(description="One or two sentences: what the pain is and who has it")
    pain_indices: list[int] = Field(description="0-based indices into the input list of pains belonging to this theme")


class ClusterResult(BaseModel):
    themes: list[Theme]


CLUSTER_SYSTEM = """You are a product-research analyst. You will receive a numbered list of
pain points mined from job boards and app reviews. Group them into themes where the underlying
problem is the same even if the wording differs. Guidelines:
- A theme should be specific enough to imagine one product solving it.
- Don't force everything into a theme: leave truly one-off pains out.
- 3 to 15 themes is typical. Each pain belongs to at most one theme."""


def cluster_with_claude(pains: list[dict], usage_sink=None) -> list[dict]:
    import anthropic
    client = anthropic.Anthropic()

    lines = [
        f"{i}. [{p['source']}] ({p['category']}, severity {p['severity']}) {p['description']}"
        for i, p in enumerate(pains)
    ]
    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=CLUSTER_SYSTEM,
        messages=[{"role": "user", "content": "Pain points:\n" + "\n".join(lines)}],
        output_format=ClusterResult,
    )
    if usage_sink:
        from .usage import from_response_usage
        usage_sink("cluster", MODEL, from_response_usage(response.usage))
    result = response.parsed_output
    if result is None:
        return []
    themes = []
    for t in result.themes:
        indices = [i for i in t.pain_indices if 0 <= i < len(pains)]
        if indices:
            themes.append({"name": t.name, "description": t.description, "indices": indices})
    return themes


def cluster_heuristic(pains: list[dict]) -> list[dict]:
    """Group by (source, category) — crude, but keeps the pipeline runnable keyless."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(pains):
        groups[p["category"] or "other"].append(i)
    return [
        {
            "name": cat.replace("_", " ").capitalize(),
            "description": f"Pains categorized as {cat} across sources.",
            "indices": idxs,
        }
        for cat, idxs in sorted(groups.items(), key=lambda kv: -len(kv[1]))
    ]


def score_theme(member_pains: list[dict]) -> dict:
    """Composite score: how often the pain appears, how badly it hurts,
    and whether independent sources corroborate it."""
    n = len(member_pains)
    sources = {p["source"] for p in member_pains}
    avg_severity = sum(p["severity"] or 1 for p in member_pains) / n
    # log-scaled frequency so one giant cluster doesn't drown everything,
    # severity weighted linearly, +25% per corroborating extra source type
    score = math.log2(1 + n) * avg_severity * (1 + 0.25 * (len(sources) - 1))
    return {
        "source_count": len(sources),
        "avg_severity": round(avg_severity, 2),
        "score": round(score, 2),
    }


def build_themes(pains: list[dict], heuristic: bool = False, usage_sink=None) -> list[dict]:
    """pains must each carry: id, source, category, severity, description.
    Returns theme dicts ready for db.replace_themes()."""
    if not pains:
        return []
    clusters = (cluster_heuristic(pains) if heuristic
                else cluster_with_claude(pains, usage_sink=usage_sink))
    themes = []
    for c in clusters:
        members = [pains[i] for i in c["indices"]]
        t = {
            "name": c["name"],
            "description": c["description"],
            "pain_ids": [pains[i]["id"] for i in c["indices"]],
            **score_theme(members),
        }
        themes.append(t)
    themes.sort(key=lambda t: -t["score"])
    return themes
