"""Streamlit dashboard: ranked pain themes with drill-down to raw evidence.

Run:  streamlit run dashboard.py
"""

import json

import streamlit as st

from painfinder import db as dbm
from painfinder import usage as usage_mod
from painfinder.sources import get_source

st.set_page_config(page_title="pain_finder", page_icon="🔍", layout="wide")

conn = dbm.connect()
stats = dbm.stats(conn)
spend = usage_mod.summary(conn)

# --- Header -------------------------------------------------------------------

st.title("pain_finder")
st.caption(
    "Pain themes mined from market evidence—job posts, product reviews, and developer "
    "issues—ranked by frequency, severity, and cross-source corroboration."
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Items collected", f"{stats['raw_items']:,}",
          help="Source documents ingested and deduplicated.")
m2.metric("Pain points", f"{stats['pains']:,}",
          help="Structured pains extracted from the raw items")
m3.metric("Themes", stats["themes"],
          help="Clusters of pains describing the same underlying problem")
m4.metric("LLM spend", f"${spend['total']['cost_usd']:,.2f}",
          help="Cumulative Claude API cost (extraction + clustering), estimated from "
               "list prices incl. batch and cache discounts")

if stats["unextracted"]:
    st.caption(f"⏳ {stats['unextracted']:,} items awaiting extraction — run "
               "`python -m painfinder.cli extract --batch`")

st.divider()

# --- Sidebar: filters + spend detail -------------------------------------------

domain_options = [r[0] for r in conn.execute(
    "SELECT DISTINCT domain FROM themes WHERE domain IS NOT NULL ORDER BY domain")]
source_options = [r[0] for r in conn.execute(
    "SELECT DISTINCT source FROM raw_items ORDER BY source")]

with st.sidebar:
    st.header("Filters")
    query = st.text_input("Search themes", placeholder="billing, sync, reconciliation…")
    selected_sources = st.multiselect(
        "Sources", source_options, format_func=lambda key: get_source(key).label,
        help="Only show themes with evidence from these sources",
    )
    selected_domains = st.multiselect("Domain", domain_options,
                                      help="Market each theme was assigned at clustering")
    loc_query = st.text_input("Location contains", placeholder="remote, US, berlin…",
                              help="Matches review store country and job-post locations; "
                                   "themes with no matching evidence are hidden")
    min_severity = st.slider("Min. avg severity", 1.0, 5.0, 1.0, 0.5)
    only_corroborated = st.toggle(
        "Corroborated only",
        help="Only themes supported by at least two independent source families",
    )

    st.divider()
    st.subheader("Spend by stage")
    if spend["stages"]:
        for s in spend["stages"]:
            st.caption(f"{s['stage']} · {s['calls']} calls · "
                       f"{s['input_tokens']:,} in / {s['output_tokens']:,} out · "
                       f"${s['cost_usd']:.3f}")
    else:
        st.caption("No Claude calls recorded yet.")

# --- Themes -------------------------------------------------------------------

themes = conn.execute("SELECT * FROM themes ORDER BY score DESC").fetchall()

if not themes:
    st.info(
        "No themes yet. Run the pipeline first:\n\n"
        "```\npython -m painfinder.cli run --domains \"crypto exchange,bookkeeping\" --batch\n```"
    )
    st.stop()


def evidence_rows(theme_id: int) -> list:
    rows = conn.execute(
        """SELECT pains.*, raw_items.source, raw_items.title AS item_title,
                  raw_items.url, raw_items.location
           FROM theme_pains
           JOIN pains ON pains.id = theme_pains.pain_id
           JOIN raw_items ON raw_items.id = pains.raw_item_id
           WHERE theme_pains.theme_id = ?
           ORDER BY pains.severity DESC""",
        (theme_id,),
    ).fetchall()
    if loc_query:
        rows = [r for r in rows if loc_query.lower() in (r["location"] or "").lower()]
    if selected_sources:
        rows = [r for r in rows if r["source"] in selected_sources]
    return rows


shown = 0
for rank, t in enumerate(themes, 1):
    if query and query.lower() not in (t["name"] or "").lower() \
            and query.lower() not in (t["description"] or "").lower():
        continue
    if (t["avg_severity"] or 0) < min_severity:
        continue
    if only_corroborated and t["source_count"] < 2:
        continue
    if selected_domains and t["domain"] not in selected_domains:
        continue
    rows = evidence_rows(t["id"])
    if (loc_query or selected_sources) and not rows:
        continue
    shown += 1

    with st.container(border=True):
        head, badge = st.columns([6, 1])
        with head:
            st.markdown(f"#### {rank}. {t['name']}")
            if t["description"]:
                st.markdown(t["description"])
            chips = []
            if t["domain"]:
                chips.append(f"`{t['domain']}`")
            chips.append(f"{t['pain_count']} pains")
            chips.append(f"severity {t['avg_severity']}")
            chips.append("🟢 corroborated" if t["source_count"] > 1 else "single source")
            st.caption(" · ".join(chips))
        with badge:
            st.metric("Score", f"{t['score']:.1f}")

        label = f"Evidence ({len(rows)}{' matching' if (loc_query or selected_sources) else ''})"
        with st.expander(label):
            for p in rows:
                source = get_source(p["source"])
                meta = [f"{source.icon} {source.label}", "🔥" * (p["severity"] or 1)]
                if p["location"]:
                    meta.append(f"📍 {p['location']}")
                tools = ", ".join(json.loads(p["tools_mentioned"] or "[]"))
                if tools:
                    meta.append(tools)
                src = ""
                if p["item_title"]:
                    src = f" — [{p['item_title']}]({p['url']})" if p["url"] else f" — {p['item_title']}"
                quote = f"\n> {p['quote'][:400]}{src}" if p["quote"] else ""
                st.markdown(f"**{p['description']}**  \n"
                            f"<span style='color:gray;font-size:0.85em'>{' · '.join(meta)}</span>"
                            f"{quote}",
                            unsafe_allow_html=True)

if shown == 0:
    st.warning("No themes match the current filters.")
else:
    st.caption(f"Showing {shown} of {len(themes)} themes.")
