"""CSS injected into the Streamlit app shell -- a minimal, neutral light theme.

Native widget colors (buttons, inputs, dataframe chrome) come from
``.streamlit/config.toml``; this module layers a small set of ``otp-*``
utility classes on top for the handful of custom blocks (hero, KPI cards,
callouts) that ``semantic_query_engine.ui.app`` and ``semantic_query_engine.ui.charts`` attach to their
own markup. No animation, gradients, blur, or icons -- flat surfaces,
borders, and type only.
"""

from __future__ import annotations

APP_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
    --otp-bg: #FFFFFF;
    --otp-panel: #FAFAFA;
    --otp-border: #E5E7EB;
    --otp-border-strong: #D1D5DB;
    --otp-text: #111827;
    --otp-muted: #6B7280;
    --otp-accent: #4F46E5;
}

html, body, .stApp {
    background-color: var(--otp-bg) !important;
    color: var(--otp-text);
    font-family: 'Inter', sans-serif;
}

[data-testid="stAppViewContainer"], [data-testid="stHeader"] {
    background: transparent !important;
}

.block-container {
    padding-top: 2rem !important;
}

h1, h2, h3 {
    font-family: 'Inter', sans-serif !important;
    font-weight: 700 !important;
    color: var(--otp-text) !important;
}

p, span, label, li { color: var(--otp-text); }
.otp-muted, small, [data-testid="stCaptionContainer"] { color: var(--otp-muted) !important; }

/* ---------------------------------------------------------------------- */
/* Sidebar                                                                 */
/* ---------------------------------------------------------------------- */

[data-testid="stSidebar"] {
    background: var(--otp-panel) !important;
    border-right: 1px solid var(--otp-border);
}

/* ---------------------------------------------------------------------- */
/* Hero / landing                                                         */
/* ---------------------------------------------------------------------- */

.otp-hero {
    padding: 28px 32px;
    border-radius: 12px;
    background: var(--otp-panel);
    border: 1px solid var(--otp-border);
    margin-bottom: 20px;
}

.otp-hero-title {
    font-weight: 700;
    font-size: 1.9rem;
    line-height: 1.2;
    margin: 0 0 8px 0;
    color: var(--otp-text);
}

.otp-hero-subtitle {
    color: var(--otp-muted);
    font-size: 1rem;
    max-width: 720px;
    margin: 0;
}

/* ---------------------------------------------------------------------- */
/* Generic card + KPI grid                                                */
/* ---------------------------------------------------------------------- */

.otp-card {
    background: var(--otp-bg);
    border: 1px solid var(--otp-border);
    border-radius: 10px;
    padding: 16px 18px;
}

.otp-kpi-value {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--otp-text);
}
.otp-kpi-label {
    color: var(--otp-muted);
    font-size: 0.76rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 4px;
}

/* ---------------------------------------------------------------------- */
/* Callout (comparison context)                                           */
/* ---------------------------------------------------------------------- */

.otp-callout {
    border-radius: 10px;
    padding: 12px 16px;
    background: var(--otp-panel);
    border: 1px solid var(--otp-border);
    font-size: 0.92rem;
}

/* ---------------------------------------------------------------------- */
/* Metric widgets + chat + tabs + expander + alerts                       */
/* ---------------------------------------------------------------------- */

div[data-testid="stMetric"] {
    background: var(--otp-bg);
    border: 1px solid var(--otp-border);
    border-radius: 10px;
    padding: 12px 16px;
}
[data-testid="stMetricLabel"] { color: var(--otp-muted) !important; }

div[data-testid="stChatMessage"] {
    border-radius: 10px;
    margin-bottom: 12px;
    border: 1px solid var(--otp-border);
    background: var(--otp-panel);
}

[data-testid="stChatInput"] {
    border-radius: 10px !important;
    border: 1px solid var(--otp-border-strong) !important;
    background: var(--otp-bg) !important;
}
[data-testid="stChatInput"]:focus-within {
    border-color: var(--otp-accent) !important;
}

button[data-baseweb="tab"] {
    font-size: 14px !important;
    font-weight: 600 !important;
    color: var(--otp-muted) !important;
}
button[aria-selected="true"] {
    color: var(--otp-text) !important;
    border-bottom-color: var(--otp-accent) !important;
}

[data-testid="stExpander"] {
    border: 1px solid var(--otp-border) !important;
    border-radius: 10px !important;
    background: var(--otp-bg) !important;
}

[data-testid="stAlert"] {
    border-radius: 10px !important;
    border: 1px solid var(--otp-border) !important;
}

.stButton > button {
    border-radius: 8px !important;
    border: 1px solid var(--otp-border-strong) !important;
    background: var(--otp-bg) !important;
    color: var(--otp-text) !important;
}
.stButton > button:hover {
    border-color: var(--otp-accent) !important;
    color: var(--otp-accent) !important;
}

div[data-testid="stStatusWidget"] {
    border-radius: 10px !important;
    border: 1px solid var(--otp-border) !important;
    background: var(--otp-bg) !important;
}

[data-testid="stDataFrame"] { border-radius: 8px; overflow: hidden; border: 1px solid var(--otp-border); }

code, pre, .stCodeBlock { border-radius: 8px !important; }
</style>
"""
