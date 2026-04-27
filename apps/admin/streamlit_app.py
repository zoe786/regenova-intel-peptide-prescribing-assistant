"""REGENOVA-Intel Admin Dashboard (7-page edition).

Streamlit application providing a full web GUI for developers:
- 💬 Chat          — query the RAG pipeline
- 📤 Upload        — upload files / register URLs for ingestion
- 📊 Ingest Jobs   — per-source job history with re-trigger
- 📚 Source Manager— browse, filter and delete ingested sources
- 🔍 Chunk Explorer— paginated chunk view with edit/delete
- 📋 Audit Logs    — searchable event log with CSV download
- ⚙️ Config        — environment and API status
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

import requests
import streamlit as st

# ── Configuration ─────────────────────────────────────────────────────────────

API_BASE_URL  = os.getenv("API_BASE_URL",  "http://localhost:8000")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "admin-dev-key")
PROCESSED_DIR = Path(os.getenv("PROCESSED_DATA_DIR", "./data/processed"))

ADMIN_HEADERS = {"X-Admin-Key": ADMIN_API_KEY}

st.set_page_config(
    page_title="REGENOVA-Intel Admin",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
.main-header { color: #0f172a; font-size: 1.9rem; font-weight: 800; letter-spacing: -.02em; }
.sub-caption  { color: #64748b; font-size: .9rem; margin-top: .2rem; }
.safety-critical { background:#fee2e2; border-left:4px solid #ef4444; padding:.5rem .75rem; border-radius:6px; margin:.3rem 0; }
.safety-warning  { background:#fef3c7; border-left:4px solid #f59e0b; padding:.5rem .75rem; border-radius:6px; margin:.3rem 0; }
.safety-info     { background:#d1fae5; border-left:4px solid #10b981; padding:.5rem .75rem; border-radius:6px; margin:.3rem 0; }
.citation-block  { background:#f8fafc; border-radius:6px; padding:.5rem; font-size:.85rem; }
.tier-badge-1 { color:#fff; background:#1a5276; padding:2px 7px; border-radius:4px; font-size:.72rem; }
.tier-badge-2 { color:#fff; background:#117a65; padding:2px 7px; border-radius:4px; font-size:.72rem; }
.tier-badge-3 { color:#fff; background:#7d6608; padding:2px 7px; border-radius:4px; font-size:.72rem; }
.tier-badge-4 { color:#fff; background:#6e2f00; padding:2px 7px; border-radius:4px; font-size:.72rem; }
.tier-badge-5 { color:#fff; background:#512e5f; padding:2px 7px; border-radius:4px; font-size:.72rem; }
.status-ok      { color:#10b981; font-weight:600; }
.status-error   { color:#ef4444; font-weight:600; }
.status-running { color:#f59e0b; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🧬 REGENOVA-Intel")
    st.markdown("*Developer Admin Dashboard*")
    st.divider()
    page = st.radio(
        "Navigation",
        [
            "💬 Chat",
            "📤 Upload",
            "📊 Ingest Jobs",
            "📚 Source Manager",
            "🔍 Chunk Explorer",
            "📋 Audit Logs",
            "⚙️ Config",
        ],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("⚠️ Decision support only. Not medical advice.")
    st.caption(f"API: `{API_BASE_URL}`")

# ── Helper Functions ───────────────────────────────────────────────────────────

def api_get(endpoint: str, params: dict | None = None) -> dict:
    try:
        resp = requests.get(
            f"{API_BASE_URL}{endpoint}",
            headers=ADMIN_HEADERS,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def api_post(endpoint: str, payload: dict | None = None, files: list | None = None) -> dict:
    try:
        if files:
            resp = requests.post(
                f"{API_BASE_URL}{endpoint}",
                headers=ADMIN_HEADERS,
                files=files,
                timeout=120,
            )
        else:
            resp = requests.post(
                f"{API_BASE_URL}{endpoint}",
                json=payload or {},
                headers=ADMIN_HEADERS,
                timeout=60,
            )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def api_delete(endpoint: str) -> dict:
    try:
        resp = requests.delete(
            f"{API_BASE_URL}{endpoint}",
            headers=ADMIN_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def api_patch(endpoint: str, payload: dict) -> dict:
    try:
        resp = requests.patch(
            f"{API_BASE_URL}{endpoint}",
            json=payload,
            headers=ADMIN_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def render_safety_flags(flags: list[dict]) -> None:
    for flag in flags:
        severity  = flag.get("severity", "info")
        css_class = f"safety-{severity}"
        icon      = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(severity, "ℹ️")
        st.markdown(
            f'<div class="{css_class}">'
            f'<strong>{icon} [{flag.get("code","?")}] {flag.get("message","")}</strong><br>'
            f'<small>{flag.get("rationale","")}</small>'
            f"</div>",
            unsafe_allow_html=True,
        )


def status_icon(status: str) -> str:
    return {"idle": "⏸️", "running": "⚙️", "completed": "✅", "failed": "❌", "queued": "🕐"}.get(status, "❓")


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 💬 Chat
# ═══════════════════════════════════════════════════════════════════════════════

if page == "💬 Chat":
    st.markdown('<h1 class="main-header">💬 Clinical Query Interface</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-caption">Submit queries to the RAG pipeline. All responses are decision support only.</p>', unsafe_allow_html=True)

    with st.form("chat_form"):
        query = st.text_area(
            "Clinical Query",
            placeholder="e.g. What is the evidence for BPC-157 in tendon healing?",
            height=110,
        )
        col1, col2, col3 = st.columns(3)
        with col1:
            role = st.selectbox("Role", ["clinician", "researcher", "admin"])
        with col2:
            top_k = st.slider("Context window (chunks)", 1, 10, 5)
        with col3:
            include_recon = st.checkbox("Include reconstitution guidance")
        submitted = st.form_submit_button("🔍 Submit Query", type="primary")

    if submitted and query.strip():
        with st.spinner("Querying RAG pipeline…"):
            result = api_post("/chat", {
                "query": query,
                "role": role,
                "context_window_size": top_k,
                "include_reconstitution": include_recon,
            })

        if "error" in result:
            st.error(f"API Error: {result['error']}")
        else:
            confidence = result.get("confidence", 0.0)
            conf_label = "🟢 High" if confidence >= 0.7 else "🟡 Medium" if confidence >= 0.4 else "🔴 Low"
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Confidence", f"{confidence:.0%}", conf_label)
            col_b.metric("Latency", f"{result.get('latency_ms',0)}ms")
            col_c.metric("Citations", len(result.get("citations", [])))
            st.divider()

            flags = result.get("safety_flags", [])
            if flags:
                st.subheader("⚠️ Safety Flags")
                render_safety_flags(flags)
                st.divider()

            st.subheader("📋 Answer")
            st.markdown(result.get("answer", "No answer generated."))

            recs = result.get("recommendations", [])
            if recs:
                st.subheader("✅ Recommendations")
                for r in recs:
                    st.markdown(f"→ {r}")

            citations = result.get("citations", [])
            if citations:
                st.divider()
                st.subheader("📚 Citations")
                for i, cit in enumerate(citations, 1):
                    tier = cit.get("evidence_tier", "?")
                    with st.expander(f"[{i}] {cit.get('source_name','Unknown')} — Tier {tier}"):
                        st.write(cit.get("excerpt", ""))
                        if cit.get("url"):
                            st.markdown(f"[🔗 Source]({cit['url']})")

            ev_summary = result.get("evidence_summary", "")
            if ev_summary:
                st.caption(f"📊 {ev_summary}")

            st.divider()
            st.warning(result.get("disclaimer", ""))
    elif submitted:
        st.warning("Please enter a query before submitting.")


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 📤 Upload
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "📤 Upload":
    st.markdown('<h1 class="main-header">📤 Upload Knowledge Sources</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-caption">Add new documents or URLs to the RAG knowledge base. Each upload triggers background ingestion.</p>', unsafe_allow_html=True)

    tab_files, tab_url = st.tabs(["📄 Upload Files", "🌐 Register URL / Video"])

    with tab_files:
        st.markdown("#### Upload PDF, TXT, or Markdown files")
        st.caption("Files are saved to `data/raw/documents/` and ingested automatically.")

        uploaded = st.file_uploader(
            "Drag & drop or click to browse",
            type=["pdf", "txt", "md"],
            accept_multiple_files=True,
            label_visibility="collapsed",
        )

        if st.button("⬆️ Upload & Ingest", type="primary", disabled=not uploaded):
            with st.spinner(f"Uploading {len(uploaded)} file(s)…"):
                files_payload = [
                    ("files", (f.name, f.read(), f.type or "application/octet-stream"))
                    for f in uploaded
                ]
                result = api_post("/upload/documents", files=files_payload)

            if "error" in result:
                st.error(f"Upload failed: {result['error']}")
            else:
                saved    = result.get("saved", [])
                rejected = result.get("rejected", [])
                job_id   = result.get("job_id", "")

                if saved:
                    st.success(f"✅ {len(saved)} file(s) uploaded — ingestion job `{job_id}` queued")
                    for f in saved:
                        st.markdown(f"- **{f['filename']}** ({f['size_bytes']:,} bytes)")
                if rejected:
                    st.warning(f"⚠️ {len(rejected)} file(s) rejected:")
                    for r in rejected:
                        st.markdown(f"- **{r['filename']}**: {r['reason']}")

    with tab_url:
        st.markdown("#### Register a URL, YouTube video, or PubMed ID")
        st.caption("The URL is appended to the source list and ingested in the background.")

        SOURCE_TYPES = [
            "website", "blog", "youtube", "forum",
            "pubmed", "skool_courses", "skool_community",
        ]

        with st.form("url_form"):
            url_input = st.text_input(
                "URL / Video ID / PMID",
                placeholder="e.g. https://example.com/article  or  dQw4w9WgXcQ  or  12345678",
            )
            col1, col2 = st.columns(2)
            with col1:
                src_type = st.selectbox("Source Type", SOURCE_TYPES)
            with col2:
                tier_override = st.selectbox(
                    "Evidence Tier Override",
                    [None, 1, 2, 3, 4, 5],
                    format_func=lambda x: "Default" if x is None else f"Tier {x}",
                )
            label = st.text_input("Label (optional)", placeholder="Human-readable name")
            url_submitted = st.form_submit_button("🌐 Register & Ingest", type="primary")

        if url_submitted:
            if not url_input.strip():
                st.warning("Please enter a URL or ID.")
            else:
                with st.spinner("Registering…"):
                    result = api_post("/upload/url", {
                        "url": url_input.strip(),
                        "source_type": src_type,
                        "evidence_tier_override": tier_override,
                        "label": label.strip() or None,
                    })
                if "error" in result:
                    st.error(f"Failed: {result['error']}")
                else:
                    st.success(
                        f"✅ `{url_input.strip()}` registered — "
                        f"ingestion job `{result.get('job_id','')}` queued"
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 📊 Ingest Jobs
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "📊 Ingest Jobs":
    st.markdown('<h1 class="main-header">📊 Ingest Job History</h1>', unsafe_allow_html=True)

    INGESTORS = ["documents", "websites", "youtube", "pubmed", "forums", "skool_courses", "skool_community"]

    col_refresh, col_trigger_all, col_trigger_single = st.columns([1, 1.5, 2])
    with col_refresh:
        if st.button("🔄 Refresh"):
            st.rerun()
    with col_trigger_all:
        if st.button("▶️ Trigger All", type="primary"):
            r = api_post("/ingest/trigger", {})
            if "error" in r:
                st.error(r["error"])
            else:
                st.success(f"All ingestors triggered — job `{r.get('job_id','')}` queued")
                time.sleep(.5)
                st.rerun()
    with col_trigger_single:
        selected_ingestor = st.selectbox("Trigger single ingestor", INGESTORS, label_visibility="collapsed")
        if st.button(f"▶️ Trigger {selected_ingestor}"):
            r = api_post(f"/ingest/trigger/{selected_ingestor}", {})
            if "error" in r:
                st.error(r["error"])
            else:
                st.success(f"`{selected_ingestor}` triggered — job `{r.get('job_id','')}` queued")
                time.sleep(.5)
                st.rerun()

    st.divider()

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        f_source = st.selectbox("Filter by source", ["all"] + INGESTORS, index=0)
    with col_f2:
        f_status = st.selectbox("Filter by status", ["", "queued", "running", "completed", "failed"], index=0)

    params: dict = {"limit": 50}
    if f_source != "all":
        params["source_type"] = f_source
    if f_status:
        params["status"] = f_status

    data = api_get("/audit/ingest-jobs", params=params)
    if "error" in data:
        st.error(f"Could not reach API: {data['error']}")
    else:
        jobs = data.get("jobs", [])
        if not jobs:
            st.info("No ingest jobs found.")
        else:
            for job in jobs:
                status    = job.get("status", "?")
                icon      = status_icon(status)
                src       = job.get("source_type", "all")
                jid       = job.get("job_id", "?")
                triggered = job.get("triggered_at", "")
                completed = job.get("completed_at") or "—"
                chunks    = job.get("total_chunks", 0)
                err       = job.get("error")

                with st.expander(
                    f"{icon} **{src}** · {triggered[:19]} · {chunks} chunks · status: {status}",
                    expanded=False,
                ):
                    st.code(f"Job ID: {jid}", language=None)
                    cols = st.columns(3)
                    cols[0].metric("Status", f"{icon} {status}")
                    cols[1].metric("Chunks", chunks)
                    cols[2].metric("Completed", completed[:19] if completed != "—" else "—")
                    if err:
                        st.error(f"Error: {err}")
                    results = job.get("results", {})
                    if results:
                        st.subheader("Per-source breakdown")
                        st.json(results)


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 📚 Source Manager
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "📚 Source Manager":
    st.markdown('<h1 class="main-header">📚 Source Manager</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-caption">Browse, filter, and delete ingested knowledge sources.</p>', unsafe_allow_html=True)

    SOURCE_TYPES_FILTER = ["", "document", "website", "youtube", "pubmed", "forum", "skool_course", "skool_community"]

    col1, col2, col3 = st.columns(3)
    with col1:
        fs_type = st.selectbox("Source type", SOURCE_TYPES_FILTER, format_func=lambda x: x or "All types")
    with col2:
        fs_tier = st.selectbox("Evidence tier", [None, 1, 2, 3, 4, 5], format_func=lambda x: "All tiers" if x is None else f"Tier {x}")
    with col3:
        if st.button("🔄 Refresh"):
            st.rerun()

    params = {"limit": 200}
    if fs_type:
        params["source_type"] = fs_type
    if fs_tier:
        params["evidence_tier"] = fs_tier

    data = api_get("/sources", params=params)

    if "error" in data:
        st.error(f"Could not reach API: {data['error']}")
    else:
        sources = data.get("sources", [])
        total   = data.get("total", 0)
        st.metric("Total Sources", total)

        if not sources:
            st.info("No sources found. Upload documents or register URLs first.")
        else:
            for src in sources:
                doc_id    = src.get("document_id", "?")
                name      = src.get("source_name", "Unknown")
                src_type  = src.get("source_type", "?")
                tier      = src.get("evidence_tier_default", "?")
                acquired  = str(src.get("acquired_at", ""))[:19]
                chunk_cnt = src.get("chunk_count", 0)
                url       = src.get("source_url")

                with st.expander(f"**{name}** · {src_type} · Tier {tier} · {chunk_cnt} chunks · {acquired}"):
                    st.code(f"Document ID: {doc_id}", language=None)
                    if url:
                        st.markdown(f"🔗 [{url}]({url})")

                    if st.button("🔍 View chunks", key=f"view_{doc_id}"):
                        st.session_state[f"view_src_{doc_id}"] = True

                    if st.session_state.get(f"view_src_{doc_id}"):
                        chunk_data = api_get("/chunks", params={"document_id": doc_id, "limit": 20})
                        if "error" not in chunk_data:
                            for chunk in chunk_data.get("chunks", []):
                                st.text(f"  [{chunk['chunk_id']}] {chunk.get('snippet','')}")
                        else:
                            st.warning(chunk_data["error"])

                    st.divider()
                    col_del, col_warn = st.columns([1, 3])
                    with col_del:
                        del_confirm = st.checkbox("Confirm delete", key=f"del_chk_{doc_id}")
                    with col_warn:
                        if del_confirm:
                            st.warning(f"This will permanently delete all {chunk_cnt} chunks for this source.")

                    if del_confirm and st.button("🗑️ Delete Source", key=f"del_{doc_id}", type="primary"):
                        result = api_delete(f"/sources/{doc_id}")
                        if "error" in result:
                            st.error(result["error"])
                        else:
                            st.success(f"✅ Deleted {result.get('chunks_deleted',0)} chunks")
                            time.sleep(.5)
                            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 🔍 Chunk Explorer
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "🔍 Chunk Explorer":
    st.markdown('<h1 class="main-header">🔍 Chunk Explorer</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-caption">Browse individual knowledge chunks; edit metadata or delete chunks.</p>', unsafe_allow_html=True)

    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        ce_search = st.text_input("Search chunks (semantic)", placeholder="e.g. BPC-157 tendon healing")
    with col2:
        ce_type = st.selectbox("Source type", ["", "document", "website", "youtube", "pubmed", "forum"], format_func=lambda x: x or "All")
    with col3:
        ce_tier = st.selectbox("Tier", [None, 1, 2, 3, 4, 5], format_func=lambda x: "All" if x is None else f"Tier {x}")
    with col4:
        ce_limit = st.selectbox("Per page", [20, 50, 100], index=0)

    if "ce_offset" not in st.session_state:
        st.session_state.ce_offset = 0

    col_prev, col_next, col_refresh = st.columns([1, 1, 2])
    with col_prev:
        if st.button("← Prev") and st.session_state.ce_offset >= ce_limit:
            st.session_state.ce_offset -= ce_limit
    with col_next:
        if st.button("Next →"):
            st.session_state.ce_offset += ce_limit
    with col_refresh:
        if st.button("🔄 Refresh"):
            st.session_state.ce_offset = 0
            st.rerun()

    params = {
        "limit": ce_limit,
        "offset": st.session_state.ce_offset,
    }
    if ce_search:
        params["search"] = ce_search
    if ce_type:
        params["source_type"] = ce_type
    if ce_tier:
        params["evidence_tier"] = ce_tier

    data = api_get("/chunks", params=params)

    if "error" in data:
        st.error(f"Could not reach API: {data['error']}")
    else:
        chunks = data.get("chunks", [])
        st.caption(f"Showing {len(chunks)} chunk(s) · page offset {st.session_state.ce_offset}")

        if not chunks:
            st.info("No chunks found for the current filters.")

        for chunk in chunks:
            cid  = chunk.get("chunk_id", "?")
            name = chunk.get("source_name", "Unknown")
            tier = chunk.get("evidence_tier_default", "?")
            snip = chunk.get("snippet", "")

            with st.expander(f"`{cid[:40]}…` — **{name}** · Tier {tier}"):
                st.markdown(f"**Snippet:** {snip}")

                if st.button("📄 Load full content", key=f"full_{cid}"):
                    full = api_get(f"/chunks/{cid}")
                    if "error" not in full:
                        st.text_area("Full content", full.get("content", ""), height=180, key=f"fc_{cid}")
                        with st.expander("Full metadata"):
                            st.json(full.get("metadata", {}))
                    else:
                        st.error(full["error"])

                st.divider()
                col_edit, col_del = st.columns(2)
                with col_edit:
                    new_tier = st.selectbox(
                        "Override evidence tier",
                        [None, 1, 2, 3, 4, 5],
                        format_func=lambda x: "No change" if x is None else f"Tier {x}",
                        key=f"tier_sel_{cid}",
                    )
                    notes_input = st.text_input("Curator notes", key=f"notes_{cid}")
                    if st.button("💾 Save changes", key=f"save_{cid}"):
                        payload: dict = {}
                        if new_tier is not None:
                            payload["evidence_tier_override"] = new_tier
                        if notes_input:
                            payload["notes"] = notes_input
                        if payload:
                            r = api_patch(f"/chunks/{cid}", payload)
                            if "error" in r:
                                st.error(r["error"])
                            else:
                                st.success(f"✅ {r.get('message','Updated')}")

                with col_del:
                    del_chk = st.checkbox("Confirm delete chunk", key=f"del_chk_{cid}")
                    if del_chk and st.button("🗑️ Delete chunk", key=f"del_{cid}", type="primary"):
                        r = api_delete(f"/chunks/{cid}")
                        if "error" in r:
                            st.error(r["error"])
                        else:
                            st.success(f"✅ {r.get('message','Deleted')}")
                            time.sleep(.3)
                            st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: 📋 Audit Logs
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "📋 Audit Logs":
    st.markdown('<h1 class="main-header">📋 Audit Logs</h1>', unsafe_allow_html=True)
    st.markdown('<p class="sub-caption">Searchable event log for all system activity. Every action is traceable.</p>', unsafe_allow_html=True)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        al_type = st.selectbox(
            "Event type",
            ["", "chat_query", "upload", "ingest_trigger", "admin_action"],
            format_func=lambda x: x or "All types",
        )
    with col2:
        al_role = st.selectbox(
            "Role",
            ["", "clinician", "researcher", "admin"],
            format_func=lambda x: x or "All roles",
        )
    with col3:
        al_since = st.text_input("Since (YYYY-MM-DD)", placeholder="2024-01-01")
    with col4:
        al_until = st.text_input("Until (YYYY-MM-DD)", placeholder="2030-12-31")

    col_req, col_lim, _ = st.columns([2, 1, 1])
    with col_req:
        al_req_prefix = st.text_input("Request ID prefix", placeholder="Optional")
    with col_lim:
        al_limit = st.selectbox("Per page", [50, 100, 200], index=0)

    params = {"limit": al_limit}
    if al_type:
        params["event_type"] = al_type
    if al_role:
        params["role"] = al_role
    if al_since:
        params["since"] = al_since
    if al_until:
        params["until"] = al_until
    if al_req_prefix:
        params["request_id_prefix"] = al_req_prefix

    data = api_get("/audit/logs", params=params)

    if "error" in data:
        st.error(f"Could not reach API: {data['error']}")
    else:
        events = data.get("events", [])
        total  = data.get("total", 0)

        col_m1, col_m2, col_dl = st.columns([1, 1, 2])
        col_m1.metric("Total matching events", total)
        col_m2.metric("Showing", len(events))

        if events:
            csv_buf = io.StringIO()
            writer  = csv.DictWriter(
                csv_buf,
                fieldnames=["id", "event_type", "timestamp", "request_id", "role", "ip_hash", "data"],
            )
            writer.writeheader()
            for ev in events:
                row = {k: ev.get(k, "") for k in ["id", "event_type", "timestamp", "request_id", "role", "ip_hash"]}
                row["data"] = json.dumps(ev.get("data", ""))
                writer.writerow(row)
            with col_dl:
                st.download_button(
                    "⬇️ Download as CSV",
                    data=csv_buf.getvalue(),
                    file_name="audit_logs.csv",
                    mime="text/csv",
                )

        st.divider()

        if not events:
            st.info("No events found for the current filters.")

        EVENT_COLORS = {
            "chat_query":     "#d1fae5",
            "upload":         "#dbeafe",
            "ingest_trigger": "#fef9c3",
            "admin_action":   "#fce7f3",
        }

        for ev in events:
            ev_type = ev.get("event_type", "?")
            ts      = str(ev.get("timestamp", ""))[:19]
            role    = ev.get("role", "")
            req_id  = ev.get("request_id", "")[:18]
            ev_data = ev.get("data", {})
            bg      = EVENT_COLORS.get(ev_type, "#f8fafc")

            with st.expander(f"**{ev_type}** · {ts} · role: {role or '—'} · {req_id}…"):
                st.markdown(
                    f"<div style='background:{bg};padding:.5rem .75rem;border-radius:6px;font-size:.8rem'>"
                    f"<b>Event:</b> {ev_type} &nbsp;|&nbsp; <b>Role:</b> {role or '—'} &nbsp;|&nbsp; "
                    f"<b>Time:</b> {ts}<br><b>Request ID:</b> {ev.get('request_id','')}<br>"
                    f"<b>IP hash:</b> {ev.get('ip_hash','—')}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                if ev_data:
                    st.json(ev_data)


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE: ⚙️ Config
# ═══════════════════════════════════════════════════════════════════════════════

elif page == "⚙️ Config":
    st.markdown('<h1 class="main-header">⚙️ Configuration</h1>', unsafe_allow_html=True)
    st.caption("Non-sensitive settings only. Secrets are not displayed.")

    health_data = api_get("/health")
    if "error" not in health_data:
        st.subheader("API Status")
        cols = st.columns(4)
        cols[0].metric("Status",      health_data.get("status", "?"))
        cols[1].metric("Version",     health_data.get("version", "?"))
        cols[2].metric("Environment", health_data.get("environment", "?"))

    ready_data = api_get("/health/ready")
    if "error" not in ready_data:
        checks = ready_data.get("checks", {})
        if checks:
            st.subheader("Readiness Checks")
            for svc, svc_status in checks.items():
                colour = "status-ok" if svc_status == "ok" else "status-error"
                icon   = "✅" if svc_status == "ok" else "❌"
                st.markdown(
                    f'<span class="{colour}">{icon} {svc}: {svc_status}</span>',
                    unsafe_allow_html=True,
                )

    st.subheader("Environment Variables (non-sensitive)")
    safe_vars = {
        "API_BASE_URL":                   API_BASE_URL,
        "LLM_MODEL":                      os.getenv("LLM_MODEL", "gpt-4o"),
        "VECTOR_DB_BACKEND":              os.getenv("VECTOR_DB_BACKEND", "chroma"),
        "ENVIRONMENT":                    os.getenv("ENVIRONMENT", "development"),
        "ENABLE_GRAPH_RETRIEVAL":         os.getenv("ENABLE_GRAPH_RETRIEVAL", "false"),
        "ENABLE_RECONSTITUTION_GUIDANCE": os.getenv("ENABLE_RECONSTITUTION_GUIDANCE", "false"),
        "AUDIT_DB_PATH":                  os.getenv("AUDIT_DB_PATH", "./data/audit.db"),
        "FRONTEND_DIR":                   os.getenv("FRONTEND_DIR", "./apps/frontend"),
    }
    for k, v in safe_vars.items():
        st.text(f"{k} = {v}")

    st.subheader("📊 Ingest Status (quick view)")
    status_data = api_get("/ingest/status")
    if "error" not in status_data:
        s = status_data.get("status", "idle")
        st.metric("Pipeline Status", f"{status_icon(s)} {s.upper()}")
        st.write(f"**Last run:** {status_data.get('last_run_at','Never')}")
        if status_data.get("error"):
            st.error(f"Last error: {status_data['error']}")
