"""Extract structured pain points from raw items.

Two engines:
  - Claude (recommended): structured extraction via the Anthropic API.
  - Heuristic: keyword rules, so the pipeline runs end-to-end without an API key.
"""

import json
import os
import re
import time

from pydantic import BaseModel, Field

from .sources import get_source, source_family

# Extraction is high-volume, per-item classification — the cheap model tier
# handles it well. Clustering/insight stays on Opus (see score.py).
EXTRACT_MODEL = os.environ.get("PAINFINDER_EXTRACT_MODEL", "claude-haiku-4-5")

CATEGORIES = [
    "manual_process", "data_integration", "tooling_gap", "pricing",
    "reliability", "performance", "usability", "support", "compliance",
    "hiring_for_pain", "other",
]


class PainPoint(BaseModel):
    description: str = Field(description="One-sentence statement of the underlying pain, phrased generically (not company-specific)")
    category: str = Field(description=f"One of: {', '.join(CATEGORIES)}")
    severity: int = Field(ge=1, le=5, description="1=mild annoyance, 5=budgeted hair-on-fire problem (a salaried hire or churn-causing failure)")
    tools_mentioned: list[str] = Field(default_factory=list, description="Products/tools named in connection with the pain")
    quote: str = Field(description="Short verbatim excerpt from the source that evidences the pain")


class ExtractionResult(BaseModel):
    pains: list[PainPoint] = Field(description="Empty if the text contains no genuine pain signal")


SYSTEM_PROMPT = """You are a product-research analyst mining source documents for
product-market-fit signals.

Follow the source-specific guidance included with each document. Extract only concrete pain:
- recurring manual work or costly workarounds
- broken or unreliable workflows
- missing capabilities and tooling or integration gaps
- pricing, support, compliance, or usability problems with meaningful consequences

Praise, vague negativity, feature announcements, administrative text, and generic implementation
work without a user problem are NOT pains. Return no pains when the evidence is weak. Rate severity
higher when people lose time, money, or data; churn; pay for a workaround; or fund a salaried role.

Phrase each pain description generically so similar pains from different sources cluster together
(e.g. "Teams manually reconcile billing data between payment and accounting systems" rather than
"Acme Corp needs someone to reconcile Stripe and NetSuite")."""


def _client():
    import anthropic
    return anthropic.Anthropic()


def extract_with_claude(source: str, title: str | None, text: str,
                        usage_sink=None) -> list[dict]:
    """Extract pains from one item using Claude structured outputs.

    usage_sink, if given, is called as usage_sink(stage, model, token_dict)
    after the API call so the caller can account for tokens/cost.
    """
    client = _client()
    user_content = _build_user_content(source, title, text)

    response = client.messages.parse(
        model=EXTRACT_MODEL,
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_content}],
        output_format=ExtractionResult,
    )
    if usage_sink:
        from .usage import from_response_usage
        usage_sink("extract", EXTRACT_MODEL, from_response_usage(response.usage))
    result = response.parsed_output
    if result is None:
        return []
    return [p.model_dump() for p in result.pains]


# --- Batch extraction (50% off — right choice for scheduled runs) -------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "pains": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                    "tools_mentioned": {"type": "array", "items": {"type": "string"}},
                    "quote": {"type": "string"},
                },
                "required": ["description", "category", "severity",
                             "tools_mentioned", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pains"],
    "additionalProperties": False,
}


def _build_user_content(source: str, title: str | None, text: str) -> str:
    definition = get_source(source)
    content = definition.document_type + (f" ({title})" if title else "")
    return (f"{content}\nSource guidance: {definition.extraction_guidance}"
            f"\n\n{text[:6000]}")


def _normalize_pains(data: dict) -> list[dict]:
    """Defensive parse of a batch result payload."""
    pains = []
    for p in data.get("pains", []):
        try:
            sev = int(p.get("severity", 1))
        except (TypeError, ValueError):
            sev = 1
        desc = str(p.get("description") or "").strip()
        if not desc:
            continue
        pains.append({
            "description": desc[:500],
            "category": p.get("category") if p.get("category") in CATEGORIES else "other",
            "severity": min(5, max(1, sev)),
            "tools_mentioned": [str(t) for t in (p.get("tools_mentioned") or [])],
            "quote": str(p.get("quote") or "")[:500],
        })
    return pains


def estimate_batch_cost_per_item(model: str | None = None) -> float:
    """Conservative pre-submission estimate (~1200 in / 300 out tokens per item)."""
    from .usage import PRICES_PER_MTOK, _DEFAULT_PRICES, BATCH_DISCOUNT
    p = PRICES_PER_MTOK.get(model or EXTRACT_MODEL, _DEFAULT_PRICES)
    return BATCH_DISCOUNT * (1200 * p["input"] + 300 * p["output"]) / 1_000_000


def extract_batch_with_claude(items: list[dict], usage_sink=None,
                              poll_seconds: int = 30,
                              timeout_minutes: int = 120) -> dict[int, list[dict]]:
    """Submit all items as one Message Batch (50% of standard price).

    items: dicts with keys id, source, title, text.
    Returns {item_id: pains} for succeeded requests; failed items are simply
    absent, so they stay unextracted and get retried on the next run.
    """
    import anthropic
    client = anthropic.Anthropic()

    requests_list = [
        {
            "custom_id": f"item-{it['id']}",
            "params": {
                "model": EXTRACT_MODEL,
                "max_tokens": 2048,
                "system": [{"type": "text", "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user",
                              "content": _build_user_content(it["source"], it["title"], it["text"])}],
                "output_config": {"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
            },
        }
        for it in items
    ]
    batch = client.messages.batches.create(requests=requests_list)
    print(f"  batch {batch.id} submitted ({len(requests_list)} items); polling...")

    deadline = time.time() + timeout_minutes * 60
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        if time.time() > deadline:
            raise RuntimeError(
                f"Batch {batch.id} still {b.processing_status} after "
                f"{timeout_minutes} min — results stay retrievable for 29 days; "
                f"re-run later or check the Console."
            )
        time.sleep(poll_seconds)

    out: dict[int, list[dict]] = {}
    from .usage import from_response_usage
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            continue
        item_id = int(result.custom_id.split("-", 1)[1])
        msg = result.result.message
        if usage_sink:
            usage_sink("extract", EXTRACT_MODEL, from_response_usage(msg.usage), batch=True)
        text = next((blk.text for blk in msg.content if blk.type == "text"), "")
        try:
            out[item_id] = _normalize_pains(json.loads(text))
        except (json.JSONDecodeError, AttributeError):
            continue
    return out


# --- Heuristic fallback (no API key required) --------------------------------

_JOB_PATTERNS = [
    (r"\bmanual(?:ly)?\b.{0,60}\b(reconcil|enter|copy|review|process|update|clean)", "manual_process", 4),
    (r"\b(spreadsheet|excel|google sheets)\b", "tooling_gap", 3),
    (r"\b(internal tool|in-house tool|build(?:ing)? tools? for)\b", "tooling_gap", 3),
    (r"\b(integrat\w+|keep .{0,30} in sync|stitch\w* .{0,20}APIs?)\b", "data_integration", 3),
    (r"\b(data entry|back[- ]office|ops backlog)\b", "manual_process", 4),
]

_REVIEW_PATTERNS = [
    (r"\b(crash\w*|freez\w*|won'?t (open|load|start))\b", "reliability", 4),
    (r"\b(lost (my |all )?(data|work|notes|progress)|deleted every)\b", "reliability", 5),
    (r"\b(sync\w*) (fail\w*|broke\w*|doesn'?t work|issues?|problems?)\b", "data_integration", 4),
    (r"\b(paywall|subscription|too expensive|price increase|used to be free)\b", "pricing", 3),
    (r"\b(slow|laggy|takes forever)\b", "performance", 2),
    (r"\b(can'?t|cannot|no way to) \w+", "usability", 2),
    (r"\b(support (never|didn'?t)|no response from support)\b", "support", 3),
]

_ISSUE_PATTERNS = [
    (r"\b(crash\w*|freez\w*|panic\w*|data loss|corrupt\w*)\b", "reliability", 4),
    (r"\b(bug|broken|regression|doesn'?t work|fails? to)\b", "reliability", 3),
    (r"\b(workaround|manually|manual process)\b", "manual_process", 3),
    (r"\b(feature request|missing|no way to|can'?t|cannot)\b", "tooling_gap", 2),
    (r"\b(integrat\w+|sync\w*|API)\b", "data_integration", 3),
    (r"\b(slow|latency|performance|takes forever)\b", "performance", 2),
]

_DISCUSSION_PATTERNS = [
    (r"\b(manually|manual process|spreadsheet|copy(?:ing)? and pasting)\b", "manual_process", 3),
    (r"\b(workaround|hacky solution|roll(?:ed)? our own|built our own)\b", "tooling_gap", 3),
    (r"\b(can'?t|cannot|no way to|missing|doesn'?t support)\b", "tooling_gap", 2),
    (r"\b(integrat\w+|keep .{0,30} in sync|sync\w* fail\w*)\b", "data_integration", 3),
    (r"\b(too expensive|pricing|price increase|costs? us)\b", "pricing", 3),
    (r"\b(crash\w*|data loss|unreliable|keeps? failing)\b", "reliability", 4),
    (r"\b(slow|latency|takes forever|performance problem)\b", "performance", 2),
]


def extract_heuristic(source: str, title: str | None, text: str) -> list[dict]:
    """Cheap keyword extraction — a rough stand-in for the Claude path."""
    family = source_family(source)
    if family == "job_posts":
        patterns = _JOB_PATTERNS
    elif family == "github_issues":
        patterns = _ISSUE_PATTERNS
    elif family in {"hn", "reddit"}:
        patterns = _DISCUSSION_PATTERNS
    else:
        patterns = _REVIEW_PATTERNS
    pains, seen_categories = [], set()
    lowered = text.lower()
    for pattern, category, severity in patterns:
        m = re.search(pattern, lowered)
        if m and category not in seen_categories:
            seen_categories.add(category)
            start = max(0, m.start() - 60)
            quote = text[start:m.end() + 60].strip()
            pains.append({
                "description": f"{category.replace('_', ' ').capitalize()} signal in {title or source}",
                "category": category,
                "severity": severity,
                "tools_mentioned": [],
                "quote": quote,
            })
    return pains


def extract(source: str, title: str | None, text: str, heuristic: bool = False,
            usage_sink=None) -> list[dict]:
    if heuristic:
        return extract_heuristic(source, title, text)
    return extract_with_claude(source, title, text, usage_sink=usage_sink)
