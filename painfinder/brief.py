"""Opportunity briefs: turn a scored theme + its raw evidence into an
actionable one-pager (who has the pain, incumbents, wedge, validation plan).

This is the judgment-heavy 'business insight' stage — it runs on the big
model, one call per brief, only when explicitly requested.
"""

import json
import os
import sqlite3

MODEL = os.environ.get("PAINFINDER_BRIEF_MODEL", "claude-opus-4-8")

BRIEF_SYSTEM = """You are a pragmatic startup analyst. You will receive one pain theme
mined from real market evidence (job posts, product reviews, developer issues, forum
discussions) with the raw evidence behind it.

Write an opportunity brief in markdown with exactly these sections:

## The pain
Who has it, when it bites, and what it costs them (time, money, churn). Ground every claim
in the evidence provided — quote short fragments where they carry weight.

## Current landscape
What sufferers use today (products named in the evidence and obvious incumbents) and why
those options are failing them, per the complaints.

## The wedge
The narrowest credible product that would relieve this pain for a specific, reachable
sub-segment. Say what it does NOT do. One paragraph.

## Willingness-to-pay signals
Evidence that money is already moving: salaried roles covering the pain, paid workarounds,
incumbent pricing complaints (people only complain about prices of things they need).
Be honest when signals are weak.

## Validate in one week
A concrete 5-step plan a solo founder could execute in 7 days with < $200: where to find
20 sufferers, what to ask, what artifact to put in front of them, and the pass/fail bar.

## Risks & honest caveats
The 2-3 most likely reasons this opportunity is worse than it looks (platform risk,
incumbent adjacency, evidence bias — e.g. review-store complaints skew consumer).

Style: direct, specific, no hype. Prefer numbers and quotes from the evidence over
generalities. If the evidence is too thin or one-sided for a section, say so plainly."""


def build_evidence_content(theme: sqlite3.Row | dict, evidence: list) -> str:
    """Assemble the user-turn content for one theme. Pure function, testable."""
    lines = [
        f"THEME: {theme['name']}",
        f"Domain: {theme['domain'] or 'cross-domain'}",
        f"Score: {theme['score']} · {theme['pain_count']} pains · "
        f"avg severity {theme['avg_severity']} · "
        f"{theme['source_count']} independent source famil(ies)",
    ]
    if theme["description"]:
        lines.append(f"Summary: {theme['description']}")
    lines.append("\nEVIDENCE (severity 1-5):")
    for p in evidence[:80]:  # cap the context; highest severity first
        tools = ", ".join(json.loads(p["tools_mentioned"] or "[]"))
        parts = [f"- [{p['source']}] (sev {p['severity']})"]
        if p["location"]:
            parts.append(f"({p['location']})")
        parts.append(p["description"])
        if tools:
            parts.append(f"| tools: {tools}")
        line = " ".join(parts)
        if p["quote"]:
            line += f'\n  quote: "{p["quote"][:220]}"'
        lines.append(line)
    return "\n".join(lines)


def theme_evidence(conn: sqlite3.Connection, theme_id: int) -> list:
    return conn.execute(
        """SELECT pains.*, raw_items.source, raw_items.location
           FROM theme_pains
           JOIN pains ON pains.id = theme_pains.pain_id
           JOIN raw_items ON raw_items.id = pains.raw_item_id
           WHERE theme_pains.theme_id = ?
           ORDER BY pains.severity DESC""",
        (theme_id,),
    ).fetchall()


def generate_brief(conn: sqlite3.Connection, theme: sqlite3.Row,
                   usage_sink=None) -> str:
    """One Opus call: theme + evidence in, opportunity brief markdown out."""
    import anthropic
    client = anthropic.Anthropic()

    evidence = theme_evidence(conn, theme["id"])
    content = build_evidence_content(theme, evidence)

    with client.messages.stream(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=BRIEF_SYSTEM,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        response = stream.get_final_message()

    if usage_sink:
        from .usage import from_response_usage
        usage_sink("brief", MODEL, from_response_usage(response.usage))

    text = "".join(b.text for b in response.content if b.type == "text")
    header = f"# Opportunity brief: {theme['name']}\n\n"
    return header + text
