"""Extract structured pain points from raw items.

Two engines:
  - Claude (recommended): structured extraction via the Anthropic API.
  - Heuristic: keyword rules, so the pipeline runs end-to-end without an API key.
"""

import re

from pydantic import BaseModel, Field

MODEL = "claude-opus-4-8"

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


SYSTEM_PROMPT = """You are a product-research analyst mining text for product-market-fit signals.

You will receive either a JOB POST (from Hacker News "Who is hiring?") or an APP REVIEW.

For a JOB POST, extract pains implied by what the company is hiring a human to do:
- Recurring manual work described in the role (reconciling, copying, triaging, cleaning data)
- Tooling gaps ("build internal tools for X", "replace our spreadsheet-based Y")
- Integration pain (keeping systems in sync, stitching APIs together)
A generic engineering role with no specific recurring task is NOT a pain — return no pains.
Rate severity higher when the pain is the role's primary purpose (they're paying a salary for it).

For an APP REVIEW, extract concrete complaints about the product:
- What broke, what's missing, what workaround the reviewer uses
- Pricing/paywall frustration, data loss, sync failures
Praise, vague negativity ("app bad"), or star ratings alone are NOT pains — return no pains.
Rate severity higher when the reviewer describes churning, losing money/data, or a paid workaround.

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
    kind = "JOB POST" if source == "hn_jobs" else "APP REVIEW"
    user_content = f"{kind}"
    if title:
        user_content += f" ({title})"
    user_content += f":\n\n{text[:6000]}"

    response = client.messages.parse(
        model=MODEL,
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
        usage_sink("extract", MODEL, from_response_usage(response.usage))
    result = response.parsed_output
    if result is None:
        return []
    return [p.model_dump() for p in result.pains]


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


def extract_heuristic(source: str, title: str | None, text: str) -> list[dict]:
    """Cheap keyword extraction — a rough stand-in for the Claude path."""
    patterns = _JOB_PATTERNS if source == "hn_jobs" else _REVIEW_PATTERNS
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
