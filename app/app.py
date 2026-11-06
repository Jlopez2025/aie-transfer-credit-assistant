"""
Transfer credit evaluator (prototype): upload a parsed transcript, review AI-drafted SNHU matches,
find missing course descriptions on the sending school's website, and approve or override.

Run:  streamlit run app.py
"""
import io, os, csv
import pandas as pd
import streamlit as st

import engine                     # sets up the matcher path and Azure settings; import first
import finder
import store
from embed_and_match import band

st.set_page_config(page_title="Transfer credit evaluator | SNHU", page_icon=":material/school:", layout="wide")

# Brand (SNHU Brand Kit): colors and Inter type live in .streamlit/config.toml. The logo is an SVG
# approximation of the reversed SNHU shield; swap in the official file when available.
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LOGO_REVERSED = os.path.join(STATIC, "snhu_logo_reversed.svg")    # full name, for the Ink Blue sidebar
SHIELD = os.path.join(STATIC, "snhu_shield.svg")                   # positive shield, collapsed-sidebar header

RESULT = {"equivalent": "Equivalent", "partial": "Partial", "not_equivalent": "Not equivalent",
          "needs description": "Needs description", "error": "Error"}
# verdict badges stay on the brand palette: Impact Blue, Deep Yellow, gray
BADGE = {"equivalent": "blue", "partial": "orange", "not_equivalent": "gray"}
SPECIAL = ["Elective credit (no direct equivalent)", "No credit"]
# Live web search for missing descriptions ("Find description") is off until it's ready for users.
# Set LIVE_SEARCH=true in app/.env to turn it back on; the search code is unchanged.
LIVE_SEARCH = finder.az.ENV.get("LIVE_SEARCH", "false").strip().lower() == "true"
SOON = "Live Agent Search Coming Soon"

# ---------------- cached data ----------------
@st.cache_resource(show_spinner="Loading SNHU catalog…")
def snhu_resource():
    return engine.load_snhu()

@st.cache_resource(show_spinner="Loading sending-school catalogs…")
def catalogs_resource():
    return engine.discover_catalogs()

@st.cache_data(show_spinner="Matching courses to SNHU…", max_entries=8)
def run_evaluation(transcript_text, found_version):
    snhu, S, cache = snhu_resource()
    docs = list(csv.DictReader(io.StringIO(transcript_text)))
    return engine.evaluate(docs, catalogs_resource(), snhu, S, cache)

SMALL = {"of", "the", "and", "at", "for", "in"}

def school(name):
    """'UNIVERSITY OF NEW HAVEN' -> 'University of New Haven'."""
    words = name.split()
    return " ".join(w.lower() if i and w.lower() in SMALL else w.capitalize() for i, w in enumerate(words))

def read_transcript(upload):
    if upload is None:
        return open(engine.SAMPLE, encoding="utf-8-sig").read()
    return upload.getvalue().decode("utf-8-sig")

def result_label(it):
    r = it.get("res")
    if r:
        return RESULT.get(r["verdict"], r["verdict"])
    h = it["handling"]
    if h.startswith("AP"):
        return "Set aside: AP/exam credit"
    if h.startswith("Withdrawn"):
        return "Set aside: withdrawn"
    if h.startswith("Not passed"):
        return "Set aside: not passed"
    return "No description"

def decision_label(d):
    if not d:
        return ""
    if d["decision"] == "approved":
        return f"Approved: {RESULT.get(d['draft_verdict'], d['draft_verdict']).lower()} {d['draft_snhu']}"
    return f"Override: {d['snhu']}"

# ---------------- sidebar ----------------
with st.sidebar:
    st.image(LOGO_REVERSED, width=220)
    st.header("Transcript")
    upload = st.file_uploader("Parsed transcript (CSV)", type=["csv"],
                              help="Columns: document_id, institution_name, program_of_study, courses_json")
    if upload is None:
        st.caption("Showing the five sample transcripts.")
    st.divider()
    st.subheader("Setup")
    # plain white text on the Ink Blue sidebar (blue badges use Ink Blue text and would vanish here)
    engines = finder.active_providers()
    if not LIVE_SEARCH:
        st.markdown("**Live Agent Search** :material/schedule:")
        st.caption("Coming soon. Searching the web for missing course descriptions is turned off.")
    elif engines:
        st.markdown("Search: **" + " → ".join("Azure agent" if e == "azure" else e.title() for e in engines)
                    + "** :material/check_circle:")
    else:
        st.markdown("Search: **no key** :material/error:")
        st.caption("Add TAVILY_API_KEY to app/.env to enable **Find description**.")
    if LIVE_SEARCH:
        st.caption("The Azure agent searches with Tavily and reads archived copies of dead or blocked pages.")
        st.caption("Searches send only the school name, course code and course title. Never student details.")

transcript_text = read_transcript(upload)
items, labels = run_evaluation(transcript_text, store.version("found_descriptions.json"))
snhu, _, _ = snhu_resource()
decisions = store.load("decisions.json")
found_store = store.load("found_descriptions.json")

with st.sidebar:
    st.subheader("Sending Catalogs in Use")
    for inst, label in sorted(labels.items()):
        st.caption(f"{school(inst)}: {label}")

# ---------------- student picker ----------------
with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
    st.image(SHIELD, width=40)
    st.title(":blue[Transfer credit evaluator]", width="content")   # blueTextColor = Ink Blue headline
st.html('<div style="width:72px;height:5px;background:#fdb913;border-radius:3px;margin:-0.25rem 0 0.25rem"></div>')
st.caption("AI-drafted matches to SNHU courses. Drafts only: an evaluator approves or overrides every line. "
           "Grade minimums and credit caps are not applied yet.")

docs = {}
for it in items:
    docs.setdefault((it["doc"], it["inst"]), []).append(it)
choice = st.selectbox("Student transcript", list(docs),
                      format_func=lambda k: f"{school(k[1])} · document {k[0]} · {len(docs[k])} lines")
rows = docs[choice]

# ---------------- summary ----------------
counts = pd.Series([result_label(it) for it in rows]).value_counts()
set_aside = int(sum(v for k, v in counts.items() if k.startswith("Set aside")))
decided = sum(store.line_key(it) in decisions for it in rows)
with st.container(horizontal=True):
    st.metric("Equivalent", int(counts.get("Equivalent", 0)), border=True)
    st.metric("Partial", int(counts.get("Partial", 0)), border=True)
    st.metric("Not equivalent", int(counts.get("Not equivalent", 0)), border=True)
    st.metric("No description", int(counts.get("No description", 0) + counts.get("Needs description", 0)), border=True)
    st.metric("Set aside", set_aside, border=True)
    st.metric("Reviewed", f"{decided} / {len(rows)}", border=True)

missing = {}
for it in rows:
    if result_label(it) in ("No description", "Needs description"):
        missing.setdefault(store.course_key(it["inst"], it["code"], it["title"]), it)

def run_find(targets):
    with st.status(f"Finding {len(targets)} description(s)…", expanded=True) as status:
        hits = 0
        for it in targets:
            try:
                rec, _ = engine.find_and_store(it["inst"], it["code"], it["title"], log=status.write, term=it["term"])
                hits += rec is not None
                status.write(f"**{it['code']} {it['title']}**: {'found' if rec else 'not found'}")
            except RuntimeError as e:
                status.update(label=str(e), state="error")
                return
        status.update(label=f"Found {hits} of {len(targets)}", state="complete")

if missing and not LIVE_SEARCH:
    st.button(SOON, icon=":material/schedule:", disabled=True, key="soon_bulk",
              help=f"{len(missing)} course(s) on this transcript have no catalog description yet.")
elif missing:
    if st.button(f"Find missing descriptions ({len(missing)})", icon=":material/travel_explore:",
                 disabled=not finder.search_ready()):
        run_find(list(missing.values()))
        st.rerun()

# ---------------- table ----------------
table = pd.DataFrame([{
    "Course": f"{it['code']} {it['title']}",
    "Term": it["term"],
    "Grade": it["grade"],
    "Result": result_label(it),
    "SNHU match": f"{it['res']['snhu']['code']} {it['res']['snhu']['title']}" if it.get("res") else "",
    "Score": round(it["res"]["score"], 2) if it.get("res") else None,
    "Description source": it["source_url"] or None,
    "Decision": decision_label(decisions.get(store.line_key(it))),
    "Flags": it.get("flags", ""),
} for it in rows])

event = st.dataframe(
    table, hide_index=True, on_select="rerun", selection_mode="single-row-required",
    key=f"table_{choice[0]}",
    column_config={
        "Course": st.column_config.TextColumn(pinned=True, width="medium"),
        "Score": st.column_config.NumberColumn(format="%.2f", help="Cosine similarity of the descriptions"),
        "Description source": st.column_config.LinkColumn(display_text=r"https?://(?:www\.)?([^/]+)"),
        "Flags": st.column_config.TextColumn(width="large"),
    },
)
sel = event.selection.rows[0] if event.selection.rows else 0
it = rows[sel]
lk = store.line_key(it)
ck = store.course_key(it["inst"], it["code"], it["title"])

# ---------------- detail ----------------
st.subheader(f"{it['code']} {it['title']}")
st.caption(f"{school(it['inst'])} · {it['term']} · grade {it['grade'] or '—'} · {'—' if it['units'] in (None, '') else it['units']} credits")
left, right = st.columns(2)

with left, st.container(border=True):
    st.markdown("**Sending Course**")
    if it["desc"]:
        how = "found by web search" if it["mode"] == "web search" else f"catalog lookup: {it['mode']}"
        st.markdown(f"{it['cat_code']} — {it['cat_title']}")
        st.caption(f"{how} · catalog {it['catalog_year'] or it.get('catalog_label') or 'year not stated'}")
        st.write(it["desc"])
        if it.get("archive"):
            st.caption(f":material/history: From the {it['archive']} copy captured {it['captured']} of "
                       f"[{finder.host(it['original_url'])}]({it['original_url']}). The live page may have changed or gone.")
        if it.get("partial"):
            st.warning("Partial description: the search service had only a summary of this page. "
                       "Open the source to read the full catalog entry before approving.", icon=":material/warning:")
        if it["source_url"]:
            st.link_button("Open source", it["source_url"], icon=":material/open_in_new:")
    elif it["handling"] and not it["handling"].startswith("Not in catalog"):
        st.info(it["handling"], icon=":material/info:")
    else:
        st.warning("No catalog description on record for this course.", icon=":material/search_off:")
    if result_label(it) in ("No description", "Needs description") and not LIVE_SEARCH:
        st.button(SOON, icon=":material/schedule:", disabled=True, key="soon_one")
    elif result_label(it) in ("No description", "Needs description") or it["mode"] == "web search":
        label = "Search again" if it["mode"] == "web search" else "Find description"
        if st.button(label, icon=":material/travel_explore:", disabled=not finder.search_ready(), key="find_one"):
            run_find([it])
            st.rerun()
    log = found_store.get(ck)
    if log and log.get("identified") and not it["desc"]:
        idc = log["identified"]
        st.info(f"Search identified this as **{idc['code']} {idc['title']}** ({idc['pages']} pages agree), "
                "but no page gave a usable description. Request that catalog entry from the school "
                "or a syllabus from the student.", icon=":material/fact_check:")
    if log:
        with st.expander(f"Search log ({len(log['tried'])} pages checked, {log['searched']})"):
            st.caption(f"Searched {log['domain'] or 'the open web'}")
            for t in log["tried"]:
                st.markdown(f"- {t.get('provider', 'tavily').title()} · [{finder.host(t['url']) or 'search'}]({t['url'] or '#'}): {t['outcome']}")

with right, st.container(border=True):
    st.markdown("**Draft SNHU Match**")
    r = it.get("res")
    if r:
        st.badge(RESULT.get(r["verdict"], r["verdict"]), color=BADGE.get(r["verdict"], "gray"))
        st.markdown(f"{r['snhu']['code']} — {r['snhu']['title']}")
        st.caption(f"Similarity {r['score']:.2f} ({band(r['score'])}) · candidate {r['rank']} of 3"
                   + (f" · confidence {r['conf']}" if r["conf"] != "" else ""))
        st.write(r["snhu"]["desc"])
        st.markdown(f"*Why:* {r['reason']}")
    else:
        st.caption("No draft match for this line.")
    if it.get("flags"):
        for fl in it["flags"].split("; "):
            st.warning(fl, icon=":material/flag:")

# ---------------- decision ----------------
with st.container(border=True):
    st.markdown("**Evaluator Decision**")
    current = decisions.get(lk)
    if current:
        st.info(f"{decision_label(current)} · {current['at']}" + (f" · {current['note']}" if current.get("note") else ""),
                   icon=":material/task_alt:")

    def save_decision(kind, snhu_label, note):
        d = store.load("decisions.json")
        d[lk] = {"decision": kind, "snhu": snhu_label, "note": note, "at": store.now(),
                 "draft_verdict": r["verdict"] if r else "", "draft_snhu": r["snhu"]["code"] if r else "",
                 "sending_source": it["source_url"]}
        store.save("decisions.json", d)

    options = SPECIAL + [f"{c['code']} {c['title']}" for c in snhu]
    with st.form(key=f"decide_{lk}", border=False):
        default = options.index(f"{r['snhu']['code']} {r['snhu']['title']}") if r and r["verdict"] != "needs description" else 0
        pick = st.selectbox("SNHU course to award (for an override)", options, index=default)
        note = st.text_input("Note", placeholder="Optional: why you approved or changed it")
        with st.container(horizontal=True):
            approve = st.form_submit_button("Approve draft", type="primary", icon=":material/check:",
                                            disabled=not r or r["verdict"] == "needs description")
            override = st.form_submit_button("Save override", icon=":material/edit:")
            clear = st.form_submit_button("Clear decision", disabled=not current, type="tertiary")
    if approve:
        save_decision("approved", f"{r['snhu']['code']} {r['snhu']['title']}", note)
        st.rerun()
    if override:
        save_decision("override", pick, note)
        st.rerun()
    if clear:
        d = store.load("decisions.json"); d.pop(lk, None); store.save("decisions.json", d)
        st.rerun()
