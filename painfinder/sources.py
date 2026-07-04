"""Source metadata shared by ingestion, extraction, scoring, and the dashboard."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDefinition:
    key: str
    label: str
    icon: str
    family: str
    document_type: str
    extraction_guidance: str


SOURCES = {
    # Legacy key retained because existing databases already contain hn_jobs rows.
    "hn_jobs": SourceDefinition(
        key="hn_jobs",
        label="HN job post",
        icon="💼",
        family="job_posts",
        document_type="JOB POST",
        extraction_guidance=(
            "Extract recurring manual work, tooling gaps, and integration pain implied by "
            "what the company is paying a person to do. Ignore generic hiring language."
        ),
    ),
    "job_posts": SourceDefinition(
        key="job_posts",
        label="Job post",
        icon="💼",
        family="job_posts",
        document_type="JOB POST",
        extraction_guidance=(
            "Extract recurring manual work, tooling gaps, and integration pain implied by "
            "what the company is paying a person to do. Ignore generic hiring language."
        ),
    ),
    "app_reviews": SourceDefinition(
        key="app_reviews",
        label="App review",
        icon="⭐",
        family="app_reviews",
        document_type="APP REVIEW",
        extraction_guidance=(
            "Extract concrete failures, missing capabilities, workarounds, pricing friction, "
            "and switching or churn triggers. Ignore praise and vague negativity."
        ),
    ),
    "github_issues": SourceDefinition(
        key="github_issues",
        label="GitHub issue",
        icon="🐙",
        family="github_issues",
        document_type="GITHUB ISSUE",
        extraction_guidance=(
            "Extract the user or developer problem behind the issue: broken workflows, missing "
            "features, integration failures, reliability problems, and costly workarounds. "
            "Ignore administrative issue text and implementation details without user pain."
        ),
    ),
    "hn": SourceDefinition(
        key="hn", label="Hacker News", icon="🟧", family="hn",
        document_type="HACKER NEWS DISCUSSION",
        extraction_guidance=(
            "Extract concrete first-hand problems, repeated workarounds, missing tools, and "
            "unmet needs. Ignore abstract debate, predictions, and second-hand speculation."
        ),
    ),
    # Registered now so future collectors inherit consistent semantics and UI labels.
    "reddit": SourceDefinition(
        key="reddit", label="Reddit", icon="🟠", family="reddit",
        document_type="REDDIT DISCUSSION",
        extraction_guidance="Extract concrete problems, repeated workarounds, and unmet needs.",
    ),
    "b2b_reviews": SourceDefinition(
        key="b2b_reviews", label="G2/Capterra review", icon="🏢", family="b2b_reviews",
        document_type="B2B SOFTWARE REVIEW",
        extraction_guidance=(
            "Extract concrete product failures, missing capabilities, workarounds, pricing "
            "friction, and switching triggers. Ignore praise and vague negativity."
        ),
    ),
}


UNKNOWN_SOURCE = SourceDefinition(
    key="unknown",
    label="Other source",
    icon="📄",
    family="unknown",
    document_type="SOURCE DOCUMENT",
    extraction_guidance="Extract concrete recurring problems, workarounds, and unmet needs.",
)


def get_source(key: str) -> SourceDefinition:
    """Return metadata for a source key, with a safe generic fallback."""
    if key in SOURCES:
        return SOURCES[key]
    return SourceDefinition(
        key=key,
        label=key.replace("_", " ").title(),
        icon=UNKNOWN_SOURCE.icon,
        family=key,
        document_type=UNKNOWN_SOURCE.document_type,
        extraction_guidance=UNKNOWN_SOURCE.extraction_guidance,
    )


def source_family(key: str) -> str:
    return get_source(key).family
