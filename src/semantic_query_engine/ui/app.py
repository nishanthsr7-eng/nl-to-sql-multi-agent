"""Streamlit chat UI for the Semantic Query Engine analytics assistant.

Run with:

    streamlit run src/semantic_query_engine/ui/app.py

after an editable install (``pip install -e .``). The sys.path fallback below
only exists so the app also boots without that install step.
"""

from __future__ import annotations

try:
    import semantic_query_engine  # noqa: F401
except ImportError:  # pragma: no cover - convenience fallback, not the primary path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import streamlit as st

from semantic_query_engine.core.config import DUCKDB_PATH, load_llm_settings
from semantic_query_engine.core.errors import WarehouseError
from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline
from semantic_query_engine.ui.catalog import EXAMPLE_QUESTIONS, apply_period_filter, recovery_ideas
from semantic_query_engine.ui.charts import (
    build_custom_chart,
    inline_markdown_to_html,
    render_metric_card,
    render_result_overview,
)
from semantic_query_engine.ui.styles import APP_CSS
from semantic_query_engine.warehouse.duckdb_client import get_connection, init_database


def ensure_database() -> None:
    if not DUCKDB_PATH.exists():
        init_database()


@st.cache_resource(show_spinner=False)
def get_shared_connection():
    """One DuckDB connection per Streamlit server process, reused across every rerun.

    ``get_connection()`` opens a fresh handle on every call; without caching it here,
    every chat interaction (a rerun of ``main()``) would leak a connection for the
    life of the process.
    """
    return get_connection()


@st.cache_data(show_spinner=False)
def get_dataset_stats(_conn) -> dict:
    """Headline warehouse stats for the landing dashboard and sidebar."""
    row = _conn.execute(
        """
        SELECT
            ROUND(SUM(units_sold * price_unit), 2) AS total_revenue,
            SUM(units_sold) AS total_units,
            COUNT(DISTINCT brand) AS brand_count,
            MIN(date) AS min_date,
            MAX(date) AS max_date
        FROM fmcg_sales
        """
    ).fetchone()
    return {
        "total_revenue": row[0] or 0,
        "total_units": row[1] or 0,
        "brand_count": row[2] or 0,
        "min_date": row[3],
        "max_date": row[4],
    }


def render_intro(conn) -> None:
    stats = get_dataset_stats(conn)

    st.markdown(
        """
        <div class="otp-hero">
            <p class="otp-hero-title">Ask anything about your FMCG sales data</p>
            <p class="otp-hero-subtitle">
                A planner, schema retriever, SQL generator, validator and synthesis agent work
                together behind the scenes to turn a plain-English question into governed SQL
                and a business-ready answer.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    kpi_cols = st.columns(4)
    with kpi_cols[0]:
        render_metric_card("Total Revenue", f"£{stats['total_revenue'] / 1_000_000:.2f}M")
    with kpi_cols[1]:
        render_metric_card("Units Sold", f"{stats['total_units']:,}")
    with kpi_cols[2]:
        render_metric_card("Brands Tracked", str(stats["brand_count"]))
    with kpi_cols[3]:
        render_metric_card("Data Range", f"{stats['min_date']} to {stats['max_date']}")

    st.markdown("#### Try one of these")
    chip_cols = st.columns(3)
    for i, (label, sample) in enumerate(EXAMPLE_QUESTIONS):
        with chip_cols[i % 3]:
            if st.button(label, key=f"landing_sample_{i}", use_container_width=True):
                st.session_state.pending_question = sample


def render_response(response, question: str | None = None, key_suffix: str = "") -> None:
    if isinstance(response, dict):
        st.markdown("---")
        if response.get("needs_clarification"):
            st.markdown(response["clarification_prompt"])
        elif response.get("error"):
            st.error("I couldn't complete that analysis safely.")
            st.write("Try one of these supported questions instead:")
            for idea in recovery_ideas(question or ""):
                st.write(f"- {idea}")
            with st.expander("Details"):
                for detail in response.get("details", []):
                    st.write(f"- {detail}")
                if response.get("sql_query"):
                    st.code(response["sql_query"], language="sql")
        elapsed_ms = response.get("elapsed_ms")
        if elapsed_ms:
            st.caption(f"{elapsed_ms / 1000:.2f}s")
        return

    st.markdown("---")

    # -- Layer 1 - Narrative Summary -------------------------------------
    st.subheader("Answer")
    st.markdown(response.narrative_summary)

    elapsed_ms = getattr(response, "elapsed_ms", 0.0) or 0.0
    if elapsed_ms:
        st.caption(f"{elapsed_ms / 1000:.2f}s")

    # -- Layer 2 & 3 - Key Metric + Comparison Context --------------------
    metric_col, comparison_col = st.columns([1, 2])
    with metric_col:
        if response.key_metric:
            render_metric_card("Key Metric", response.key_metric)
    with comparison_col:
        if response.comparison_context:
            callout = inline_markdown_to_html(response.comparison_context)
            st.markdown(f'<div class="otp-callout">{callout}</div>', unsafe_allow_html=True)

    # -- Layer 4 - Chart + Data -------------------------------------------
    if response.result_table:
        results = pd.DataFrame(response.result_table)
        render_result_overview(results)

        chart_tab, data_tab = st.tabs(["Visual Analysis", "Data Table"])

        with chart_tab:
            st.caption(f"Showing all {len(results):,} rows returned by the query.")

            numeric_cols   = list(results.select_dtypes(include="number").columns)
            dimension_cols = [col for col in results.columns if col not in numeric_cols]

            if not numeric_cols:
                st.info("This result has no numeric columns to plot.")
            elif not dimension_cols:
                st.info("This result has no dimension columns to use as X-axis.")
            else:
                ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns(4)

                rec = (response.chart_recommendation or "bar").lower()
                default_type_idx = {"line": 1, "area": 2, "scatter": 3, "pie": 4}.get(rec, 0)

                chart_type = ctrl_col1.selectbox(
                    "Chart Type", ["Bar", "Line", "Area", "Scatter", "Pie"],
                    index=default_type_idx,
                    key=f"type_{key_suffix}_{abs(hash(response.sql_query))}"
                )
                x_col = ctrl_col2.selectbox(
                    "X-Axis", dimension_cols, index=0,
                    key=f"x_{key_suffix}_{abs(hash(response.sql_query))}"
                )
                y_col = ctrl_col3.selectbox(
                    "Y-Axis", numeric_cols, index=len(numeric_cols) - 1,
                    key=f"y_{key_suffix}_{abs(hash(response.sql_query))}"
                )
                other_dims = ["None"] + [col for col in dimension_cols if col != x_col]
                color_col = ctrl_col4.selectbox(
                    "Color By", other_dims, index=0,
                    key=f"color_{key_suffix}_{abs(hash(response.sql_query))}"
                )

                chart = build_custom_chart(results, chart_type, x_col, y_col, color_col)
                if chart is not None:
                    st.altair_chart(chart, use_container_width=True)
                else:
                    st.info("The selected combination could not be rendered.")

        with data_tab:
            st.caption(f"{len(results):,} rows - {len(results.columns)} fields")
            st.dataframe(results, use_container_width=True, hide_index=True, height=520)
            st.download_button(
                "Download as CSV",
                results.to_csv(index=False).encode("utf-8"),
                file_name="sqe_analysis.csv",
                mime="text/csv",
                key=f"download_{key_suffix}_{abs(hash(response.sql_query))}",
            )

    # -- Details: SQL + agent trace, collapsed by default ------------------
    with st.expander("Details"):
        st.caption("The exact SQL query and agent trace behind this answer.")
        st.code(response.sql_query, language="sql")
        for step in response.agent_trace:
            st.write(step)


def render_sidebar(conn) -> str:
    """Render the sidebar and return the selected data period."""
    with st.sidebar:
        st.markdown("**Semantic Query Engine**")
        st.caption("AI Analytics Assistant")

        settings = load_llm_settings()
        # "configured", not "connected": a key being present doesn't mean a call
        # to the provider has actually succeeded yet -- that's only known once a
        # question is asked, and any failure there degrades to fallback silently.
        status_text = (
            f"LLM configured -- {settings.generator_model}"
            if settings.is_enabled
            else "Fallback mode -- deterministic templates"
        )
        st.caption(status_text)

        if st.button("New chat", use_container_width=True):
            st.session_state.messages = []
            st.session_state.query_cache = {}
            st.rerun()

        selected_period = st.selectbox(
            "Data period", ["All available data", "2022", "2023", "2024"],
            help="Applied only when the question does not already contain a year.",
        )

    return selected_period


def main() -> None:
    st.set_page_config(page_title="Semantic Query Engine", layout="wide")
    st.markdown(APP_CSS, unsafe_allow_html=True)

    try:
        ensure_database()
        conn = get_shared_connection()
    except WarehouseError as exc:
        st.error(
            "Semantic Query Engine couldn't open its data warehouse and can't continue. "
            "This usually means the DuckDB file is locked by another process "
            "or has been corrupted."
        )
        st.caption(str(exc))
        st.stop()
        return

    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "pipeline" not in st.session_state:
        # Reuses the same process-wide cached connection as get_dataset_stats /
        # the sidebar, rather than each session opening its own via
        # AnalyticsPipeline()'s default -- see AnalyticsPipeline.__init__.
        st.session_state.pipeline = AnalyticsPipeline(conn=conn)
    if "query_cache" not in st.session_state:
        st.session_state.query_cache = {}

    selected_period = render_sidebar(conn=conn)

    st.title("Semantic Query Engine -- AI Analytics Assistant")
    st.caption(
        "Natural language FMCG analytics with RAG-grounded SQL generation and guided business insights."
    )

    if not st.session_state.messages:
        render_intro(conn)

    for i, message in enumerate(st.session_state.messages):
        with st.chat_message(message["role"]):
            if message["role"] == "assistant":
                prior_question = st.session_state.messages[i - 1]["content"] if i > 0 else None
                render_response(message["content"], question=prior_question, key_suffix=str(i))
            else:
                st.write(message["content"])

    question = st.chat_input("Ask a business question about FMCG sales...")
    if "pending_question" in st.session_state:
        question = st.session_state.pop("pending_question")

    if question:
        question = apply_period_filter(question, selected_period)
        st.session_state.messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.write(question)

        with st.chat_message("assistant"):
            with st.status("Analyzing your question...", expanded=False) as status:
                cache_key = question.strip().lower()
                if cache_key in st.session_state.query_cache:
                    response = st.session_state.query_cache[cache_key]
                    status.update(label="Loaded a previous analysis", state="complete", expanded=False)
                else:
                    # Prior user turns, so a clarification follow-up like "revenue, last
                    # month" can be understood in light of the question that prompted it.
                    prior_turns = [
                        m["content"] for m in st.session_state.messages[:-1] if m["role"] == "user"
                    ]
                    response = st.session_state.pipeline.run(question, context=prior_turns)
                    st.session_state.query_cache[cache_key] = response
                    status.update(label="Analysis complete", state="complete", expanded=False)
            render_response(response, question, key_suffix=str(len(st.session_state.messages)))
        st.session_state.messages.append({"role": "assistant", "content": response})


if __name__ == "__main__":
    main()
