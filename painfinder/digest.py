"""Markdown digest: what changed since the last run.

Deterministic (no LLM calls) — reads themes, theme_history, and llm_usage.
Used by the `digest` CLI command and published as the GitHub Actions job
summary after every scheduled run.
"""

import sqlite3
from datetime import datetime, timezone

from . import db as dbm
from . import usage as usage_mod


def _fmt_delta(delta: float | None) -> str:
    if delta is None:
        return "🆕 new"
    if abs(delta) < 0.05:
        return "→"
    arrow = "▲" if delta > 0 else "▼"
    return f"{arrow} {delta:+.1f}"


def build_digest(conn: sqlite3.Connection, top_n: int = 10) -> str:
    stats = dbm.stats(conn)
    spend = usage_mod.summary(conn)["total"]
    latest, previous = dbm.last_two_snapshots(conn)
    themes = conn.execute(
        "SELECT * FROM themes ORDER BY score DESC").fetchall()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# pain_finder digest — {today}",
        "",
        f"**{stats['raw_items']:,}** items · **{stats['pains']:,}** pains · "
        f"**{stats['themes']}** themes · lifetime LLM spend **${spend['cost_usd']:.2f}**",
        "",
    ]

    # --- Top themes table ---
    lines += [f"## Top {min(top_n, len(themes))} themes", "",
              "| # | Theme | Domain | Score | Δ | Pains | Corroborated |",
              "|---|---|---|---|---|---|---|"]
    for rank, t in enumerate(themes[:top_n], 1):
        prev = previous.get(t["name"])
        delta = (t["score"] - prev["score"]) if prev else (None if previous else 0.0)
        corr = "✅" if (t["source_count"] or 0) > 1 else ""
        lines.append(
            f"| {rank} | {t['name']} | {t['domain'] or '—'} | {t['score']:.1f} "
            f"| {_fmt_delta(delta)} | {t['pain_count']} | {corr} |"
        )
    lines.append("")

    # --- Movers & new themes (only meaningful with two snapshots) ---
    if previous:
        movers = []
        for name, row in latest.items():
            if name in previous:
                movers.append((name, row["score"] - previous[name]["score"], row))
        movers.sort(key=lambda m: -abs(m[1]))
        significant = [m for m in movers if abs(m[1]) >= 0.5][:8]
        if significant:
            lines += ["## Biggest movers", ""]
            for name, delta, row in significant:
                lines.append(f"- {_fmt_delta(delta)} **{name}** "
                             f"({row['domain'] or 'cross-domain'}) — now {row['score']:.1f}")
            lines.append("")

        new_names = [n for n in latest if n not in previous]
        if new_names:
            lines += ["## New themes", ""]
            for n in sorted(new_names, key=lambda n: -latest[n]["score"])[:10]:
                row = latest[n]
                lines.append(f"- **{n}** ({row['domain'] or 'cross-domain'}) — "
                             f"score {row['score']:.1f}, {row['pain_count']} pains")
            lines.append("")

        gone = [n for n in previous if n not in latest]
        if gone:
            lines.append(f"*{len(gone)} theme(s) from the previous run were merged or "
                         f"renamed by re-clustering.*")
            lines.append("")
    else:
        lines += ["*First snapshot — trend deltas will appear from the next run.*", ""]

    return "\n".join(lines)
