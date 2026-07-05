"""SQLite storage for raw items, extracted pains, and scored themes."""

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.environ.get("PAINFINDER_DB", "data/painfinder.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,              -- source key from painfinder.sources
    external_id TEXT NOT NULL,         -- comment id / review id
    title TEXT,                        -- app name or job post first line
    author TEXT,
    rating INTEGER,                    -- reviews only (1-5)
    text TEXT NOT NULL,
    url TEXT,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    extracted INTEGER NOT NULL DEFAULT 0,
    domain TEXT,                       -- market/domain this item was collected for
    location TEXT,                     -- country code (reviews) or parsed location (job posts)
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS pains (
    id INTEGER PRIMARY KEY,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
    description TEXT NOT NULL,
    category TEXT,
    severity INTEGER,                  -- 1 (mild annoyance) .. 5 (budgeted, hair-on-fire)
    tools_mentioned TEXT,              -- JSON array
    quote TEXT,                        -- supporting excerpt from the source text
    extracted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    domain TEXT,                       -- market/domain label assigned at clustering
    pain_count INTEGER NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    avg_severity REAL,
    score REAL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_pains (
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    pain_id INTEGER NOT NULL REFERENCES pains(id),
    PRIMARY KEY (theme_id, pain_id)
);

CREATE TABLE IF NOT EXISTS theme_history (
    id INTEGER PRIMARY KEY,
    snapshot_at TEXT NOT NULL,         -- one timestamp per scoring run
    name TEXT NOT NULL,
    domain TEXT,
    score REAL,
    pain_count INTEGER,
    source_count INTEGER,
    avg_severity REAL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY,
    theme_name TEXT NOT NULL,
    domain TEXT,
    score REAL,                        -- theme score when the brief was written
    content TEXT NOT NULL,             -- markdown
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,               -- 'extract' | 'cluster'
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created before a column existed."""
    for table, col, decl in [("raw_items", "domain", "TEXT"), ("themes", "domain", "TEXT"),
                             ("raw_items", "location", "TEXT")]:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.commit()


def connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def insert_raw_items(conn: sqlite3.Connection, items: list[dict]) -> int:
    """Insert items, skipping duplicates. Returns number of new rows."""
    inserted = 0
    for it in items:
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_items
               (source, external_id, title, author, rating, text, url, posted_at,
                fetched_at, domain, location)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                it["source"], it["external_id"], it.get("title"), it.get("author"),
                it.get("rating"), it["text"], it.get("url"), it.get("posted_at"),
                now_iso(), it.get("domain"), it.get("location"),
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def unextracted_items(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    q = "SELECT * FROM raw_items WHERE extracted = 0 ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    return conn.execute(q).fetchall()


def save_pains(conn: sqlite3.Connection, raw_item_id: int, pains: list[dict]) -> None:
    for p in pains:
        conn.execute(
            """INSERT INTO pains (raw_item_id, description, category, severity,
                                  tools_mentioned, quote, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                raw_item_id, p["description"], p.get("category"), p.get("severity"),
                json.dumps(p.get("tools_mentioned", [])), p.get("quote"), now_iso(),
            ),
        )
    conn.execute("UPDATE raw_items SET extracted = 1 WHERE id = ?", (raw_item_id,))
    conn.commit()


def all_pains(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT pains.*, raw_items.source, raw_items.title AS item_title,
                  raw_items.url, raw_items.posted_at, raw_items.domain, raw_items.location
           FROM pains JOIN raw_items ON raw_items.id = pains.raw_item_id
           ORDER BY pains.id"""
    ).fetchall()


def replace_themes(conn: sqlite3.Connection, themes: list[dict]) -> None:
    """themes: [{name, description, pain_ids, avg_severity, source_count, score}]

    Also appends a snapshot of every theme to theme_history, so scores can be
    tracked across runs (rising pains = opportunities)."""
    conn.execute("DELETE FROM theme_pains")
    conn.execute("DELETE FROM themes")
    # Microsecond precision: two scoring runs in the same second must not
    # merge into one snapshot.
    snapshot_at = datetime.now(timezone.utc).isoformat()
    for t in themes:
        conn.execute(
            """INSERT INTO theme_history (snapshot_at, name, domain, score,
                                          pain_count, source_count, avg_severity)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_at, t["name"], t.get("domain"), t["score"],
             len(t["pain_ids"]), t["source_count"], t["avg_severity"]),
        )
    for t in themes:
        cur = conn.execute(
            """INSERT INTO themes (name, description, domain, pain_count, source_count,
                                   avg_severity, score, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                t["name"], t.get("description"), t.get("domain"), len(t["pain_ids"]),
                t["source_count"], t["avg_severity"], t["score"], now_iso(),
            ),
        )
        theme_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO theme_pains (theme_id, pain_id) VALUES (?, ?)",
            [(theme_id, pid) for pid in t["pain_ids"]],
        )
    conn.commit()


def last_two_snapshots(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Returns ({name: row} for the latest snapshot, same for the previous one).
    Either dict may be empty. Themes are matched across runs by exact name."""
    times = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_at FROM theme_history ORDER BY snapshot_at DESC LIMIT 2"
    )]
    out = []
    for ts in times:
        rows = conn.execute(
            "SELECT * FROM theme_history WHERE snapshot_at = ?", (ts,)).fetchall()
        out.append({r["name"]: r for r in rows})
    while len(out) < 2:
        out.append({})
    return out[0], out[1]


def theme_score_history(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT snapshot_at, score FROM theme_history
           WHERE name = ? ORDER BY snapshot_at""", (name,)).fetchall()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def save_brief(conn: sqlite3.Connection, theme_name: str, domain: str | None,
               score: float | None, content: str) -> None:
    conn.execute(
        """INSERT INTO briefs (theme_name, domain, score, content, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (theme_name, domain, score, content, now_iso()),
    )
    conn.commit()


def latest_brief(conn: sqlite3.Connection, theme_name: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM briefs WHERE theme_name = ?
           ORDER BY created_at DESC LIMIT 1""", (theme_name,)).fetchone()


def stats(conn: sqlite3.Connection) -> dict:
    row = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    return {
        "raw_items": row("SELECT COUNT(*) FROM raw_items"),
        "unextracted": row("SELECT COUNT(*) FROM raw_items WHERE extracted = 0"),
        "pains": row("SELECT COUNT(*) FROM pains"),
        "themes": row("SELECT COUNT(*) FROM themes"),
    }
