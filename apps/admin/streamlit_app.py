"""REGENOVA-Intel Admin Dashboard.

Streamlit application providing a web UI for:
- Chat interface (query the RAG pipeline)
- Ingest status monitoring
- Source browser
- Configuration viewer
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
import streamlit as st

# ── Configuration ─────────────────────────────────────────────────────────────

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "admin-dev-key")
PROCESSED_DIR = Path(os.getenv("PROCESSED_DATA_DIR", "./data/processed"))

st.set_page_config(
    page_title="REGENOVA-Intel Admin",
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Styling ───────────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    .main-header { color: #1a5276; font-size: 2rem; font-weight: 700; }
    .safety-critical { background-color: #fdedec; border-left: 4px solid #e74c3c; padding: 0.5rem; }
    .safety-warning { background-color: #fef9e7; border-left: 4px solid #f39c12; padding: 0.5rem; }
    .safety-info { background-color: #eaf4fb; border-left: 4px solid #3498db; padding: 0.5rem; }
    .citation-block { background-color: #f8f9fa; border-radius: 4px; padding: 0.5rem; font-size: 0.85rem; }
    .tier-badge-1 { color: white; background-color: #1a5276; padding: 2px 6px; border-radius: 3px; font-size: 0.75rem; }
    .tier-badge-3 { color: white; background-color: #117a65; padding: 2px 6px; border-radius: 3px; font-size: 0.75rem; }
    .tier-badge-4 { color: white; background-color: #7d6608; padding: 2px 6px; border-radius: 3px; font-size: 0.75rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Sidebar Navigation ────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 🧬 REGENOVA-Intel")
    st.markdown("*Clinical Decision Support*")
    st.divider()
    page = st.radio(
        "Navigation",
        ["💬 Chat", "📊 Ingest Status", "📚 Source Browser", "⚙️ Config"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("⚠️ Decision support only. Not medical advice.")


# ── Helper Functions ──────────────────────────────────────────────────────────

def api_get(endpoint: str) -> dict:
    """Make a GET request to the API."""
    try:
        resp = requests.get(
            f"{API_BASE_URL}{endpoint}",
            headers={"X-Admin-Key": ADMIN_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def api_post(endpoint: str, payload: dict) -> dict:
    """Make a POST request to the API."""
    try:
        resp = requests.post(
            f"{API_BASE_URL}{endpoint}",
            json=payload,
            headers={"X-Admin-Key": ADMIN_API_KEY},
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"error": str(e)}


def render_safety_flags(flags: list[dict]) -> None:
    """Render safety flags with severity-based styling."""
    for flag in flags:
        severity = flag.get("severity", "info")
        css_class = f"safety-{severity}"
        icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(severity, "ℹ️")
        st.markdown(
            f'<div class="{css_class}">'
            f'<strong>{icon} [{flag.get("code", "?")}] {flag.get("message", "")}</strong><br>'
            f'<small>{flag.get("rationale", "")}</small>'
            f"</div>",
            unsafe_allow_html=True,
        )


# ── Pages ─────────────────────────────────────────────────────────────────────

if page == "💬 Chat":
    st.markdown('<h1 class="main-header">💬 Clinical Query Interface</h1>', unsafe_allow_html=True)
    st.caption("Submit queries to the RAG pipeline. All responses are decision support only.")

    with st.form("chat_form"):
        query = st.text_area(
            "Clinical Query",
            placeholder="e.g. What is the evidence for BPC-157 in tendon healing?",
            height=100,
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
            # Confidence meter
            confidence = result.get("confidence", 0.0)
            conf_label = "🟢 High" if confidence >= 0.7 else "🟡 Medium" if confidence >= 0.4 else "🔴 Low"
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Confidence", f"{confidence:.0%}", conf_label)
            col_b.metric("Latency", f"{result.get('latency_ms', 0)}ms")
            col_c.metric("Citations", len(result.get("citations", [])))

            st.divider()

            # Safety flags
            flags = result.get("safety_flags", [])
            if flags:
                st.subheader("⚠️ Safety Flags")
                render_safety_flags(flags)
                st.divider()

            # Answer
            st.subheader("📋 Answer")
            st.markdown(result.get("answer", "No answer generated."))

            # Citations
            citations = result.get("citations", [])
            if citations:
                st.divider()
                st.subheader("📚 Citations")
                for i, cit in enumerate(citations, 1):
                    with st.expander(f"[{i}] {cit.get('source_name', 'Unknown')} — Tier {cit.get('evidence_tier', '?')}"):
                        st.write(cit.get("excerpt", ""))
                        if cit.get("url"):
                            st.markdown(f"[🔗 Source]({cit['url']})")

            # Evidence summary
            ev_summary = result.get("evidence_summary", "")
            if ev_summary:
                st.caption(f"📊 {ev_summary}")

            # Disclaimer
            st.divider()
            st.warning(result.get("disclaimer", ""))

    elif submitted:
        st.warning("Please enter a query before submitting.")


elif page == "📊 Ingest Status":
    st.markdown('<h1 class="main-header">📊 Ingestion Pipeline Status</h1>', unsafe_allow_html=True)

    col1, col2 = st.columns([2, 1])
    with col2:
        if st.button("🔄 Refresh Status"):
            st.rerun()
        if st.button("▶️ Trigger Ingestion", type="primary"):
            result = api_post("/ingest/trigger", {})
            if "error" in result:
                st.error(result["error"])
            else:
                st.success(result.get("message", "Triggered"))

    status_data = api_get("/ingest/status")

    if "error" in status_data:
        st.error(f"Could not reach API: {status_data['error']}")
    else:
        status = status_data.get("status", "unknown")
        icon = {"idle": "⏸️", "running": "⚙️", "completed": "✅", "failed": "❌"}.get(status, "❓")
        st.metric("Pipeline Status", f"{icon} {status.upper()}")
        st.write(f"**Last run:** {status_data.get('last_run_at', 'Never')}")

        if status_data.get("error"):
            st.error(f"Last error: {status_data['error']}")

        if status_data.get("last_result"):
            st.subheader("Last Run Result")
            st.json(status_data["last_result"])


elif page == "📚 Source Browser":
    st.markdown('<h1 class="main-header">📚 Source Browser</h1>', unsafe_allow_html=True)

    normalized_dir = PROCESSED_DIR / "normalized"
    if not normalized_dir.exists():
        st.warning(f"Normalized data directory not found: {normalized_dir}")
    else:
        json_files = list(normalized_dir.glob("*.json"))
        st.metric("Normalized Chunks", len(json_files))

        if json_files:
            selected = st.selectbox(
                "Select a chunk to inspect",
                options=[f.name for f in json_files[:100]],
            )
            if selected:
                filepath = normalized_dir / selected
                try:
                    data = json.loads(filepath.read_text())
                    cols = st.columns(3)
                    cols[0].metric("Source Type", data.get("metadata", {}).get("source_type", "?"))
                    cols[1].metric("Evidence Tier", data.get("metadata", {}).get("evidence_tier_default", "?"))
                    cols[2].metric("Content Length", len(data.get("content", "")))
                    st.subheader("Content")
                    st.text_area("Chunk content", data.get("content", ""), height=200)
                    st.subheader("Metadata")
                    st.json(data.get("metadata", {}))
                except Exception as e:
                    st.error(f"Failed to load file: {e}")
        else:
            st.info("No normalized chunks found. Run ingestion first.")


elif page == "⚙️ Config":
    st.markdown('<h1 class="main-header">⚙️ Configuration</h1>', unsafe_allow_html=True)
    st.caption("Non-sensitive settings only. Secrets are not displayed.")

    health_data = api_get("/health")
    if "error" not in health_data:
        st.subheader("API Status")
        cols = st.columns(4)
        cols[0].metric("Status", health_data.get("status", "?"))
        cols[1].metric("Version", health_data.get("version", "?"))
        cols[2].metric("Environment", health_data.get("environment", "?"))

    st.subheader("Environment Variables (non-sensitive)")
    safe_vars = {
        "API_BASE_URL": API_BASE_URL,
        "LLM_MODEL": os.getenv("LLM_MODEL", "gpt-4o"),
        "VECTOR_DB_BACKEND": os.getenv("VECTOR_DB_BACKEND", "chroma"),
        "ENVIRONMENT": os.getenv("ENVIRONMENT", "development"),
        "ENABLE_GRAPH_RETRIEVAL": os.getenv("ENABLE_GRAPH_RETRIEVAL", "false"),
        "ENABLE_RECONSTITUTION_GUIDANCE": os.getenv("ENABLE_RECONSTITUTION_GUIDANCE", "false"),
    }
    for k, v in safe_vars.items():
        st.text(f"{k} = {v}")
