# pain_finder 🔍

Mine **job boards** and **app reviews** for product-market-fit signals: recurring pains that
companies pay salaries to work around, and complaints that drive users away from incumbent
products. Rank the resulting themes so you can see which problems are worth building for.

**What each source tells you:**

- **Job posts** (HN "Who is hiring?") are a *demand* signal — when many companies hire humans
  to "manually reconcile X with Y" or "build internal tools for Z", that's a budgeted,
  recurring pain with no good product solving it.
- **App reviews** (App Store) are a *dissatisfaction* signal — clustering critical reviews of
  incumbents by complaint theme shows you wedges into an existing market.

> Honest caveat: this finds *problems worth building for*. True PMF measurement needs your own
> product's retention/usage data — no external dataset can give you that.

## Pipeline

```
ingest (HN Algolia API, App Store RSS — both free & keyless)
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

No API keys are needed for ingestion — the HN Algolia API and Apple's review RSS feeds are
public. Without an Anthropic key you can still run the whole pipeline with `--heuristic`
(keyword rules instead of Claude — much lower quality, useful for smoke-testing).

## Usage

```bash
# 1. Ingest the latest HN Who-is-Hiring thread (~monthly, hundreds of posts)
python -m painfinder.cli ingest-hn

# 2. Ingest critical reviews for incumbent apps in the space you're exploring
python -m painfinder.cli ingest-reviews --app "notion"
python -m painfinder.cli ingest-reviews --app "quickbooks" --max-rating 3

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
2. **Edit the app list / budget** at the top of the workflow file (`REVIEW_APPS`, `BUDGET_USD`).
3. Trigger the first run manually from the **Actions** tab (workflow_dispatch) to backfill.
   After that it fires on the daily cron. Note: scheduled workflows only run on the repo's
   **default branch**, so merge this branch first if it isn't the default.

For the dashboard, deploy to **Streamlit Community Cloud** (free): share.streamlit.io →
New app → point it at this repo and `dashboard.py`. It reads `data/painfinder.db` from the
repo, and redeploys automatically whenever the Actions run pushes fresh data.

Prefer your own server instead? The cron line above is all you need — the app is a plain
Python process with a SQLite file, no other infrastructure.

## Costs

**Ingestion is free** — the HN Algolia API and Apple's review feeds are public and the
ingest stage makes no LLM calls. The paid stages are **extract** and **score** (Claude API),
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
- **Corroboration** boosts themes that show up in *both* job posts and reviews — a pain that
  companies hire for *and* users complain about is the strongest signal here.

The dashboard always shows the raw quotes and links behind each score, so you can validate
rather than trust a number.

## Extending

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
