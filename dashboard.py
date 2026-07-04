"""Streamlit dashboard: ranked pain themes with drill-down to raw evidence.

Run:  streamlit run dashboard.py
"""

import json

import streamlit as st

from painfinder import db as dbm
from painfinder import usage as usage_mod

st.set_page_config(page_title="pain_finder", page_icon="🔍", layout="wide")

conn = dbm.connect()
stats = dbm.stats(conn)
spend = usage_mod.summary(conn)

# --- Header -------------------------------------------------------------------

st.title("pain_finder")
st.markdown(
    "Pain themes mined from **job boards** (what companies pay people to do manually) "
    "and **app reviews** (what drives users away from incumbents), ranked by frequency, "
    "severity, and cross-source corroboration."
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Items collected", f"{stats['raw_items']:,}",
          help="Job posts + reviews ingested (deduplicated)")
m2.metric("Pain points", f"{stats['pains']:,}",
          help="Structured pains extracted from the raw items")
m3.metric("Themes", stats["themes"],
          help="Clusters of pains describing the same underlying problem")
m4.metric("LLM spend", f"${spend['total']['cost_usd']:,.2f}",
          help="Cumulative Claude API cost across extraction + clustering "
               "(ingestion is free). Estimated from list prices incl. batch "
               "and cache discounts.")

if stats["unextracted"]:
    st.caption(f"⏳ {stats['unextracted']:,} items awaiting extraction — run "
               f"`python -m painfinder.cli extract --batch`")

# --- Sidebar: filters + spend detail -------------------------------------------

with st.sidebar:
    st.header("Filters")
    query = st.text_input("Search themes", placeholder="e.g. billing, sync, spreadsheet")
    min_severity = st.slider("Min. avg severity", 1.0, 5.0, 1.0, 0.5)
    only_corroborated = st.toggle(
        "Corroborated only",
        help="Show only themes seen in BOTH job posts and app reviews — the strongest signal",
    )

    st.divider()
    st.subheader("Spend by stage")
    if spend["stages"]:
        for s in spend["stages"]:
            st.markdown(
                f"**{s['stage']}** · {s['calls']} calls · ${s['cost_usd']:.3f}  \n"
                f"<span style='color:gray;font-size:0.85em'>"
                f"{s['input_tokens']:,} in / {s['output_tokens']:,} out tokens</span>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("No Claude calls recorded yet.")

# --- Themes -------------------------------------------------------------------

themes = conn.execute("SELECT * FROM themes ORDER BY score DESC").fetchall()

if not themes:
    st.info(
        "No themes yet. Run the pipeline first:\n\n"
        "```\npython -m painfinder.cli run --apps \"notion,quickbooks\" --batch\n```"
    )
    st.stop()

max_score = max(t["score"] for t in themes) or 1.0

shown = 0
for rank, t in enumerate(themes, 1):
    if query and query.lower() not in (t["name"] or "").lower() \
            and query.lower() not in (t["description"] or "").lower():
        continue
    if (t["avg_severity"] or 0) < min_severity:
        continue
    if only_corroborated and t["source_count"] < 2:
        continue
    shown += 1

    with st.container(border=True):
        head, badge = st.columns([5, 1])
        with head:
            st.subheader(f"{rank}. {t['name']}")
            if t["description"]:
                st.markdown(t["description"])
        with badge:
            st.metric("Score", f"{t['score']:.1f}")

        facts = f"**{t['pain_count']}** pains · avg severity **{t['avg_severity']}**"
        if t["source_count"] > 1:
            facts += " · 🟢 **corroborated** (job posts *and* reviews)"
        else:
            facts += " · single source type"
        st.markdown(facts)

        with st.expander(f"Evidence ({t['pain_count']})"):
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
                source_label = "💼 job post" if p["source"] == "hn_jobs" else "⭐ review"
                sev = "🔥" * (p["severity"] or 1)
                tools = ", ".join(json.loads(p["tools_mentioned"] or "[]"))
                st.markdown(f"**{p['description']}**")
                meta = f"{source_label} · severity {sev}"
                if tools:
                    meta += f" · tools: {tools}"
                st.caption(meta)
                if p["quote"]:
                    src = f" — [{p['item_title']}]({p['url']})" if p["url"] else \
                          (f" — {p['item_title']}" if p["item_title"] else "")
                    st.markdown(f"> {p['quote'][:400]}{src}")
                st.markdown("")

if shown == 0:
    st.warning("No themes match the current filters.")
else:
    st.caption(f"Showing {shown} of {len(themes)} themes.")
