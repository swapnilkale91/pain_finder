"""LLM token/cost accounting. Every Claude call is recorded in the llm_usage table."""

import sqlite3

from .db import now_iso

# $ per million tokens (Claude API list prices)
PRICES_PER_MTOK = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
}
_DEFAULT_PRICES = PRICES_PER_MTOK["claude-opus-4-8"]


def cost_usd(model: str, tokens: dict) -> float:
    p = PRICES_PER_MTOK.get(model, _DEFAULT_PRICES)
    return (
        tokens.get("input_tokens", 0) * p["input"]
        + tokens.get("output_tokens", 0) * p["output"]
        + tokens.get("cache_write_tokens", 0) * p["cache_write"]
        + tokens.get("cache_read_tokens", 0) * p["cache_read"]
    ) / 1_000_000


def from_response_usage(u) -> dict:
    """Map an Anthropic response.usage object to our token dict.

    Note: the API's input_tokens is the UNcached remainder — cache tokens
    are reported separately, so summing these fields never double-counts.
    """
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
    }


def record(conn: sqlite3.Connection, stage: str, model: str, tokens: dict) -> float:
    """Persist one call's usage; returns its estimated cost in USD."""
    cost = cost_usd(model, tokens)
    conn.execute(
        """INSERT INTO llm_usage (stage, model, input_tokens, output_tokens,
                                  cache_write_tokens, cache_read_tokens, cost_usd, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            stage, model,
            tokens.get("input_tokens", 0), tokens.get("output_tokens", 0),
            tokens.get("cache_write_tokens", 0), tokens.get("cache_read_tokens", 0),
            cost, now_iso(),
        ),
    )
    conn.commit()
    return cost


def summary(conn: sqlite3.Connection) -> dict:
    """Totals overall and per stage."""
    total = conn.execute(
        """SELECT COUNT(*) AS calls,
                  COALESCE(SUM(input_tokens + cache_write_tokens + cache_read_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cost_usd), 0) AS cost_usd
           FROM llm_usage"""
    ).fetchone()
    stages = conn.execute(
        """SELECT stage, COUNT(*) AS calls,
                  SUM(input_tokens + cache_write_tokens + cache_read_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cost_usd) AS cost_usd
           FROM llm_usage GROUP BY stage ORDER BY cost_usd DESC"""
    ).fetchall()
    return {"total": dict(total), "stages": [dict(s) for s in stages]}
