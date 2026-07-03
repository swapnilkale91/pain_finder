"""Streamlit dashboard: ranked pain themes with drill-down to raw evidence.

Run:  streamlit run dashboard.py
"""

import json

import streamlit as st

from painfinder import db as dbm

st.set_page_config(page_title="pain_finder", page_icon="🔍", layout="wide")
st.title("🔍 pain_finder")
st.caption("Pain themes mined from job boards and app reviews — ranked by frequency, severity, and cross-source corroboration.")

conn = dbm.connect()
stats = dbm.stats(conn)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Raw items", stats["raw_items"])
c2.metric("Awaiting extraction", stats["unextracted"])
c3.metric("Pain points", stats["pains"])
c4.metric("Themes", stats["themes"])

themes = conn.execute("SELECT * FROM themes ORDER BY score DESC").fetchall()

if not themes:
    st.info(
        "No themes yet. Run the pipeline first:\n\n"
        "```\npython -m painfinder.cli ingest-hn\n"
        "python -m painfinder.cli ingest-reviews --app notion\n"
        "python -m painfinder.cli extract\n"
        "python -m painfinder.cli score\n```"
    )
    st.stop()

st.divider()

for rank, t in enumerate(themes, 1):
    header = f"**#{rank} — {t['name']}**  ·  score {t['score']}  ·  {t['pain_count']} pains  ·  avg severity {t['avg_severity']}"
    if t["source_count"] > 1:
        header += "  ·  ✅ corroborated across source types"
    with st.expander(header, expanded=(rank <= 3)):
        if t["description"]:
            st.write(t["description"])
        rows = conn.execute(
            """SELECT pains.*, raw_items.source, raw_items.title AS item_title, raw_items.url
               FROM theme_pains
               JOIN pains ON pains.id = theme_pains.pain_id
               JOIN raw_items ON raw_items.id = pains.raw_item_id
               WHERE theme_pains.theme_id = ?
               ORDER BY pains.severity DESC""",
            (t["id"],),
        ).fetchall()
        for p in rows:
            source_label = "💼 job post" if p["source"] == "hn_jobs" else "⭐ app review"
            tools = ", ".join(json.loads(p["tools_mentioned"] or "[]"))
            st.markdown(f"- **{p['description']}** &nbsp; `{source_label}` `severity {p['severity']}`"
                        + (f" `{tools}`" if tools else ""))
            if p["quote"]:
                st.caption(f"“{p['quote'][:300]}”" + (f" — [{p['item_title']}]({p['url']})" if p["url"] else f" — {p['item_title'] or ''}"))
