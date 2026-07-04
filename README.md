# pain_finder 🔍

Mine market evidence for product-market-fit signals: recurring pains that companies pay salaries
to work around, complaints that drive users away from incumbent products, and developer issues
that expose broken or missing workflows. Rank the resulting themes so you can see which problems
are worth building for.

**What each source tells you:**

- **Job posts** (HN "Who is hiring?") are a *demand* signal — when many companies hire humans
  to "manually reconcile X with Y" or "build internal tools for Z", that's a budgeted,
  recurring pain with no good product solving it.
- **App reviews** (App Store) are a *dissatisfaction* signal — clustering critical reviews of
  incumbents by complaint theme shows you wedges into an existing market.
- **GitHub Issues** are a *developer pain* signal — bugs, feature requests, and documented
  workarounds expose gaps in technical products and integrations.

> Honest caveat: this finds *problems worth building for*. True PMF measurement needs your own
> product's retention/usage data — no external dataset can give you that.

## Pipeline

```
ingest (HN Algolia API, App Store RSS, GitHub REST API)
  → extract (Claude turns each item into structured pain points)
  → score (Claude clusters pains into themes; deterministic composite score)
  → dashboard (Streamlit: ranked themes with raw evidence behind each)
```

Everything lands in a local SQLite DB (`data/painfinder.db`).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # for extraction & clustering
```

No API keys are needed for public-source ingestion. Set `GITHUB_TOKEN` for higher GitHub API
limits or authorized private repositories. Without an Anthropic key you can still run the whole
pipeline with `--heuristic` (keyword rules instead of Claude—much lower quality, useful for
smoke-testing).

## Usage

```bash
# 1. Ingest the latest HN Who-is-Hiring thread (~monthly, hundreds of posts)
python -m painfinder.cli ingest-hn

# 2a. Ingest critical reviews for specific incumbent apps
python -m painfinder.cli ingest-reviews --app "notion"

# 2b. Or sweep an entire domain: top 10 App Store matches, tagged with the domain
python -m painfinder.cli ingest-domain "crypto exchange"
python -m painfinder.cli ingest-domain "bookkeeping" --top 15
python -m painfinder.cli ingest-domain "crypto on/off-ramp" --query "buy crypto"

# 2c. Discover relevant repositories, then sample their issues (pull requests are excluded)
python -m painfinder.cli ingest-github --query "bookkeeping" --domain "bookkeeping" --max 200

# Repository-specific collection is also available
python -m painfinder.cli ingest-github --repo "owner/repo" --labels "bug,performance" --domain "observability"

# Location: --country pulls a different App Store storefront (tagged on every item);
# job posts get their location parsed from the "Company | Role | Location" first line
python -m painfinder.cli ingest-domain "bookkeeping" --country in

# 3. Extract structured pain points (uses Claude; add --limit 50 to control spend)
python -m painfinder.cli extract

# 4. Cluster into themes and score them
python -m painfinder.cli score

# 5. Explore
streamlit run dashboard.py
```

`stats` shows pipeline counts at any point. Re-running ingest is idempotent (dedupe on
source + external ID); `extract` only processes items it hasn't seen.

## Automatic ingestion

`run` does the whole pipeline in one command — ingest everything, extract only the new
items, re-score:

```bash
# One shot, with a $5 extraction spend cap (the default)
python -m painfinder.cli run --apps "notion,quickbooks" --budget-usd 5

# Every --domains entry also drives GitHub repository discovery automatically
python -m painfinder.cli run --domains "crypto exchange,bookkeeping" --batch

# Keep it running: re-check sources every 12 hours
python -m painfinder.cli run --apps "notion,quickbooks" --loop --interval-hours 12
```

Or schedule the one-shot form with cron (survives reboots, unlike `--loop`):

```cron
0 8 * * * cd /path/to/pain_finder && .venv/bin/python -m painfinder.cli run --apps "notion,quickbooks" >> pipeline.log 2>&1
```

The HN thread is monthly and review feeds move slowly, so daily is plenty.

### Running it in the cloud (no server needed)

The repo ships a GitHub Actions workflow (`.github/workflows/pipeline.yml`) that runs the
full pipeline **daily** and commits the updated SQLite DB back to the repo. To enable it:

1. **Add your API key as a secret:** repo → Settings → Secrets and variables → Actions →
   New repository secret → name `ANTHROPIC_API_KEY`.
2. **Edit the source lists / budget** at the top of the workflow file (`REVIEW_APPS`,
   `DOMAINS`, and `BUDGET_USD`). Each domain drives both App Store and GitHub collection.
3. Trigger the first run manually from the **Actions** tab (workflow_dispatch) to backfill.
   After that it fires on the daily cron. Note: scheduled workflows only run on the repo's
   **default branch**, so merge this branch first if it isn't the default.

For the dashboard, deploy to **Streamlit Community Cloud** (free): share.streamlit.io →
New app → point it at this repo and `dashboard.py`. It reads `data/painfinder.db` from the
repo, and redeploys automatically whenever the Actions run pushes fresh data.

Prefer your own server instead? The cron line above is all you need — the app is a plain
Python process with a SQLite file, no other infrastructure.

## Costs

**Ingestion is free**—the HN Algolia API, Apple's review feeds, and GitHub's public Issues API are
public, and the ingest stage makes no LLM calls. The paid stages are **extract** and **score** (Claude API),
and each stage uses the cheapest model that's good enough:

| Stage | Model | Why |
|---|---|---|
| extract (per-item) | Haiku 4.5 (`PAINFINDER_EXTRACT_MODEL` to override) | Simple classification, high volume — 5× cheaper than Opus |
| cluster + insight | Opus 4.8 (`PAINFINDER_CLUSTER_MODEL` to override) | One judgment-heavy call over everything |

Add `--batch` to `extract`/`run` to route extraction through the Message Batches API —
**50% off** on top, at the cost of minutes of latency (irrelevant for scheduled runs; the
GitHub Actions workflow uses it). Typical costs with Haiku + batch:

- Full HN thread (~500 posts): roughly **$0.30**
- Critical reviews for one app: **a few cents**
- Clustering (one Opus call per `score` run): **$0.10–0.50** depending on pain count

Because ingest is deduplicated, follow-up runs only pay for *new* items — a daily `run`
after the initial backfill typically costs cents. Guardrails and visibility:

- `--budget-usd N` on `extract`/`run` hard-stops extraction at the cap (resume by re-running)
- every Claude call's tokens and estimated cost land in the `llm_usage` table
- `python -m painfinder.cli usage` prints totals per stage; the dashboard shows
  cumulative tokens and spend in the header

## Scoring

Each theme's score = `log2(1 + pain_count) × avg_severity × (1 + 0.25 × extra_source_types)`.

- **Frequency** is log-scaled so one giant cluster doesn't drown the rest.
- **Severity** (1–5) is judged at extraction time — 5 means a budgeted, hair-on-fire problem
  (a salaried hire, or churn-causing data loss).
- **Corroboration** boosts themes that show up across independent source families—for example,
  a pain that companies hire for, users complain about, and developers report in issues.

The dashboard always shows the raw quotes and links behind each score, so you can validate
rather than trust a number.

## Extending

The next-version source set is Hacker News discussions, Reddit, app reviews, GitHub Issues,
job posts, and G2/Capterra reviews. GitHub Issues is now supported; the source registry already
defines the semantics and display metadata that future collectors plug into.

- **More sources:** add a module under `painfinder/ingest/` that returns
  `{source, external_id, title, text, ...}` dicts — the rest of the pipeline is source-agnostic.
  Natural next candidates: Greenhouse/Lever public job boards (open JSON per company),
  Trustpilot API, Reddit. (Note: G2, Glassdoor, and LinkedIn prohibit scraping — use official
  APIs or licensed aggregators like Coresignal/TheirStack for those.)
- **Trends over time:** ingest is dated; add a monthly cron and chart theme scores per month.
- **Willingness-to-pay:** join salary bands from job posts and incumbent pricing onto themes.

## Development

```bash
python -m pytest tests/
```

Tests cover the parsing and scoring logic with recorded API fixtures — no network or API key
required.
