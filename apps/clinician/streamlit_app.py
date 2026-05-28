"""REGENOVA-Intel Clinician Chat UI.

A compact, end-user-focused chat interface for clinicians.
Runs on port 8502 by default.

Environment variables:
    API_BASE_URL   — FastAPI base URL (default: http://localhost:8000)
"""

from __future__ import annotations

import html
import os
import time
from urllib.parse import urlparse

import requests
import streamlit as st

# ── Configuration ──────────────────────────────────────────────────────────────

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
_CHAT_ENDPOINT = f"{API_BASE_URL}/chat"
_HEALTH_ENDPOINT = f"{API_BASE_URL}/health"

# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="REGENOVA-Intel · Clinician Assistant",
    page_icon="💊",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ── CSS ────────────────────────────────────────────────────────────────────────

st.markdown(
    """
<style>
/* ── Global resets ── */
html, body, [data-testid="stAppViewContainer"] {
    background: #f0f4f8 !important;
}
[data-testid="stSidebar"] { display: none; }

/* ── App wrapper: constrain to narrow panel ── */
[data-testid="stMain"] > div:first-child {
    max-width: 680px;
    margin: 0 auto;
    padding: 0 !important;
}
.block-container {
    max-width: 680px !important;
    padding: 0 1rem 1rem !important;
}

/* ── Compact header ── */
.rg-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #0f4c75;
    color: #fff;
    border-radius: 10px 10px 0 0;
    padding: .6rem 1rem;
    margin-bottom: .5rem;
    position: sticky;
    top: 0;
    z-index: 100;
}
.rg-header-title {
    font-size: 1rem;
    font-weight: 700;
    letter-spacing: -.01em;
    margin: 0;
}
.rg-header-sub {
    font-size: .72rem;
    opacity: .75;
    margin: .1rem 0 0;
}
.rg-status-online  { color: #6ee7b7; font-size: .78rem; font-weight: 600; }
.rg-status-offline { color: #fca5a5; font-size: .78rem; font-weight: 600; }

/* ── Disclaimer banner ── */
.rg-disclaimer {
    background: #fff7ed;
    border-left: 3px solid #f59e0b;
    border-radius: 0 6px 6px 0;
    padding: .45rem .75rem;
    font-size: .75rem;
    color: #78350f;
    margin-bottom: .75rem;
}

/* ── Confidence badge ── */
.conf-high   { background:#d1fae5; color:#065f46; padding:2px 8px; border-radius:12px; font-size:.75rem; font-weight:600; }
.conf-medium { background:#fef3c7; color:#78350f; padding:2px 8px; border-radius:12px; font-size:.75rem; font-weight:600; }
.conf-low    { background:#fee2e2; color:#7f1d1d; padding:2px 8px; border-radius:12px; font-size:.75rem; font-weight:600; }

/* ── Evidence tier badges ── */
.tier-1 { background:#1a5276; color:#fff; padding:1px 6px; border-radius:4px; font-size:.7rem; }
.tier-2 { background:#0e6655; color:#fff; padding:1px 6px; border-radius:4px; font-size:.7rem; }
.tier-3 { background:#7d6608; color:#fff; padding:1px 6px; border-radius:4px; font-size:.7rem; }
.tier-4 { background:#6e2f00; color:#fff; padding:1px 6px; border-radius:4px; font-size:.7rem; }
.tier-5 { background:#512e5f; color:#fff; padding:1px 6px; border-radius:4px; font-size:.7rem; }

/* ── Safety flags ── */
.safety-critical {
    background: #fee2e2; border-left: 3px solid #ef4444;
    padding: .4rem .7rem; border-radius: 0 6px 6px 0;
    font-size: .8rem; margin: .25rem 0; color: #7f1d1d;
}
.safety-warning {
    background: #fef3c7; border-left: 3px solid #f59e0b;
    padding: .4rem .7rem; border-radius: 0 6px 6px 0;
    font-size: .8rem; margin: .25rem 0; color: #78350f;
}
.safety-info {
    background: #d1fae5; border-left: 3px solid #10b981;
    padding: .4rem .7rem; border-radius: 0 6px 6px 0;
    font-size: .8rem; margin: .25rem 0; color: #065f46;
}

/* ── Citation pill strip ── */
.citation-strip {
    display: flex;
    flex-wrap: wrap;
    gap: .3rem;
    margin-top: .4rem;
}
.citation-pill {
    background: #e0f2fe;
    color: #0c4a6e;
    border-radius: 20px;
    padding: 2px 10px;
    font-size: .72rem;
    font-weight: 500;
    white-space: nowrap;
    cursor: default;
}

/* ── Chat bubbles ── */
[data-testid="stChatMessage"] {
    border-radius: 10px !important;
    padding: .5rem .75rem !important;
}

/* ── Compact expander ── */
[data-testid="stExpander"] summary {
    font-size: .8rem !important;
    color: #475569 !important;
}

/* ── Input area ── */
[data-testid="stChatInput"] {
    border-top: 1px solid #e2e8f0;
    padding-top: .5rem;
}
</style>
""",
    unsafe_allow_html=True,
)

# ── Backend health check ───────────────────────────────────────────────────────


@st.cache_data(ttl=30, show_spinner=False)
def _check_health() -> bool:
    """Return True if the API backend is reachable."""
    try:
        r = requests.get(_HEALTH_ENDPOINT, timeout=4)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ── Session state ──────────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []  # list of dicts {role, content, meta}

# ── Header ─────────────────────────────────────────────────────────────────────

online = _check_health()
status_cls = "rg-status-online" if online else "rg-status-offline"
status_txt = "● API online" if online else "● API offline"

st.markdown(
    f"""
<div class="rg-header">
  <div>
    <p class="rg-header-title">💊 REGENOVA-Intel</p>
    <p class="rg-header-sub">Peptide Prescribing Assistant · Clinician</p>
  </div>
  <span class="{status_cls}">{status_txt}</span>
</div>
""",
    unsafe_allow_html=True,
)

# ── Disclaimer banner ──────────────────────────────────────────────────────────

st.markdown(
    """
<div class="rg-disclaimer">
  ⚠️ <strong>Clinical decision support only.</strong>
  All outputs must be reviewed by a qualified clinician before application.
</div>
""",
    unsafe_allow_html=True,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _conf_badge(confidence: float) -> str:
    if confidence >= 0.7:
        pct = round(confidence * 100)
        return f'<span class="conf-high">● {pct}% confidence</span>'
    if confidence >= 0.4:
        pct = round(confidence * 100)
        return f'<span class="conf-medium">● {pct}% confidence</span>'
    pct = round(confidence * 100)
    return f'<span class="conf-low">● {pct}% confidence</span>'


def _tier_badge(tier: int) -> str:
    t = max(1, min(tier, 5))
    labels = {1: "Tier 1 · RCT", 2: "Tier 2 · Cohort", 3: "Tier 3 · Case", 4: "Tier 4 · Opinion", 5: "Tier 5 · Other"}
    return f'<span class="tier-{t}">{labels[t]}</span>'


def _safety_html(flag: dict) -> str:
    raw_sev = str(flag.get("severity", "info")).lower()
    sev = raw_sev if raw_sev in {"critical", "warning", "info"} else "info"
    icon = {"critical": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(sev, "ℹ️")
    cls = f"safety-{sev}"
    code = _escape_html(flag.get("code", ""))
    msg = _escape_html(flag.get("message", ""))
    rationale = _escape_html(flag.get("rationale", ""))
    return (
        f'<div class="{cls}">'
        f"<strong>{icon} {code}</strong> {msg}"
        + (f"<br><span style='font-size:.72rem;opacity:.85'>{rationale}</span>" if rationale else "")
        + "</div>"
    )


def _escape_html(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_external_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return None


def _render_assistant_meta(meta: dict) -> None:
    """Render confidence, safety flags, citations, and evidence summary for an answer."""
    confidence = meta.get("confidence", 0.0)
    citations = meta.get("citations", [])
    safety_flags = meta.get("safety_flags", [])
    evidence_summary = meta.get("evidence_summary", "")
    recommendations = meta.get("recommendations", [])

    # Confidence + evidence summary in one line
    conf_html = _conf_badge(confidence)
    ev_text = f"<span style='font-size:.75rem;color:#64748b;margin-left:.6rem'>{evidence_summary}</span>" if evidence_summary else ""
    st.markdown(conf_html + ev_text, unsafe_allow_html=True)

    # Safety flags — always visible if present
    if safety_flags:
        for flag in safety_flags:
            st.markdown(_safety_html(flag), unsafe_allow_html=True)

    # Recommendations
    if recommendations:
        with st.expander("📋 Recommendations", expanded=False):
            for rec in recommendations:
                st.markdown(f"- {rec}")

    # Citations as compact pill strip + expandable detail
    if citations:
        pill_html = '<div class="citation-strip">'
        for c in citations:
            name = _escape_html(c.get("source_name", "Source"))
            pill_html += f'<span class="citation-pill">{name}</span>'
        pill_html += "</div>"
        st.markdown(pill_html, unsafe_allow_html=True)

        with st.expander(f"📚 Evidence sources ({len(citations)})", expanded=False):
            for i, c in enumerate(citations, 1):
                tier = c.get("evidence_tier", 5)
                name = _escape_html(c.get("source_name", "Unknown"))
                excerpt = c.get("excerpt", "")
                url = _safe_external_url(c.get("url"))
                source_id = _escape_html(c.get("source_id", ""))
                label = f"**{i}. {name}**" + (f" — [{source_id}]({url})" if url else f" — `{source_id}`")
                st.markdown(
                    f"{_tier_badge(tier)} &nbsp; {label}",
                    unsafe_allow_html=True,
                )
                if excerpt:
                    st.caption(f'"{excerpt}"')
                if i < len(citations):
                    st.divider()


# ── Chat transcript ────────────────────────────────────────────────────────────

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("meta"):
            _render_assistant_meta(msg["meta"])

# ── Composer (sticky bottom via Streamlit chat_input) ─────────────────────────

query = st.chat_input(
    "Ask a clinical question…",
    disabled=not online,
)

if query:
    # Append user message
    st.session_state.messages.append({"role": "user", "content": query, "meta": None})
    with st.chat_message("user"):
        st.markdown(query)

    # Call API
    with st.chat_message("assistant"):
        with st.spinner("Retrieving evidence…"):
            try:
                t0 = time.monotonic()
                resp = requests.post(
                    _CHAT_ENDPOINT,
                    json={
                        "query": query,
                        "role": "clinician",
                        "context_window_size": 5,
                        "include_reconstitution": False,
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                elapsed = int((time.monotonic() - t0) * 1000)
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                data = {"error": f"API returned HTTP {status_code}"}
            except requests.RequestException as exc:
                data = {"error": str(exc)}

        if "error" in data:
            answer = f"⚠️ Could not retrieve a response: {data['error']}"
            meta: dict = {}
        else:
            answer = data.get("answer", "*(no answer returned)*")
            meta = {
                "confidence": data.get("confidence", 0.0),
                "citations": data.get("citations", []),
                "safety_flags": data.get("safety_flags", []),
                "evidence_summary": data.get("evidence_summary", ""),
                "recommendations": data.get("recommendations", []),
            }

        st.markdown(answer)
        if meta:
            _render_assistant_meta(meta)

    # Persist to session
    st.session_state.messages.append(
        {"role": "assistant", "content": answer, "meta": meta if meta else None}
    )

# ── Clear conversation button ──────────────────────────────────────────────────

if st.session_state.messages and st.button("🗑 Clear conversation", use_container_width=True, type="secondary"):
        st.session_state.messages = []
        st.rerun()
