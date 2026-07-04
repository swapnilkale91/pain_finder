"""Cluster extracted pains into themes and score them.

Clustering engines mirror extraction: Claude (semantic) or heuristic (by category).
Scoring is deterministic either way.
"""

import math
import os
from collections import defaultdict

from pydantic import BaseModel, Field

# Clustering/insight is the judgment-heavy stage — keep it on the big model.
MODEL = os.environ.get("PAINFINDER_CLUSTER_MODEL", "claude-opus-4-8")


class Theme(BaseModel):
    name: str = Field(description="Short name for the pain theme, 3-8 words")
    description: str = Field(description="One or two sentences: what the pain is and who has it")
    domain: str = Field(description="Short market/domain label, e.g. 'crypto payments', 'accounting software'; 'cross-domain' if it spans industries")
    pain_indices: list[int] = Field(description="0-based indices into the input list of pains belonging to this theme")


class ClusterResult(BaseModel):
    themes: list[Theme]


CLUSTER_SYSTEM = """You are a product-research analyst. You will receive a numbered list of
pain points mined from job boards and app reviews. Group them into themes where the underlying
problem is the same even if the wording differs. Guidelines:
- A theme should be specific enough to imagine one product solving it.
- Don't force everything into a theme: leave truly one-off pains out.
- Each pain belongs to at most one theme.
- Assign every theme a short domain label: the market or industry where the pain lives
  (e.g. 'crypto payments', 'accounting software', 'field service', 'developer tools').
  Some pains carry a [domain: ...] tag from collection — trust it but refine when the
  content is more specific. Use 'cross-domain' only when the pain genuinely spans
  industries. Prefer splitting a theme by domain over one vague cross-domain theme:
  'reconciliation pain in crypto on/off-ramps' and 'reconciliation pain in e-commerce
  bookkeeping' are more actionable than 'reconciliation pain'."""


# Hand-written schema (structured outputs require additionalProperties: false)
CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "domain": {"type": "string"},
                    "pain_indices": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "description", "domain", "pain_indices"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}


def cluster_with_claude(pains: list[dict], usage_sink=None) -> list[dict]:
    """One clustering call over all pains.

    Streams with a large max_tokens: adaptive thinking shares the output
    budget, and on many hundreds of pains it can consume 10k+ tokens before
    the JSON starts — a small cap truncates the JSON mid-string.
    """
    import anthropic
    client = anthropic.Anthropic()

    lines = []
    for i, p in enumerate(pains):
        tag = f" [domain: {p['domain']}]" if p.get("domain") else ""
        lines.append(f"{i}. [{p['source']}]{tag} ({p['category']}, severity {p['severity']}) "
                     f"{p['description']}")
    with client.messages.stream(
        model=MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        system=CLUSTER_SYSTEM,
        messages=[{"role": "user", "content": "Pain points:\n" + "\n".join(lines)}],
        output_config={"format": {"type": "json_schema", "schema": CLUSTER_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    # Record spend before parsing — a truncated/failed parse is still billed.
    if usage_sink:
        from .usage import from_response_usage
        usage_sink("cluster", MODEL, from_response_usage(response.usage))

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Clustering output hit max_tokens and was truncated; "
            "raise max_tokens in score.py or cluster fewer pains at once."
        )
    text = next(b.text for b in response.content if b.type == "text")
    result = ClusterResult.model_validate_json(text)
    themes = []
    for t in result.themes:
        indices = [i for i in t.pain_indices if 0 <= i < len(pains)]
        if indices:
            themes.append({"name": t.name, "description": t.description,
                           "domain": t.domain, "indices": indices})
    return themes


def _majority_domain(members: list[dict]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for p in members:
        if p.get("domain"):
            counts[p["domain"]] += 1
    if not counts:
        return "cross-domain"
    return max(counts.items(), key=lambda kv: kv[1])[0]


def cluster_heuristic(pains: list[dict]) -> list[dict]:
    """Group by (source, category) — crude, but keeps the pipeline runnable keyless."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(pains):
        groups[p["category"] or "other"].append(i)
    return [
        {
            "name": cat.replace("_", " ").capitalize(),
            "description": f"Pains categorized as {cat} across sources.",
            "domain": _majority_domain([pains[i] for i in idxs]),
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
            "domain": c.get("domain"),
            "pain_ids": [pains[i]["id"] for i in c["indices"]],
            **score_theme(members),
        }
        themes.append(t)
    themes.sort(key=lambda t: -t["score"])
    return themes
