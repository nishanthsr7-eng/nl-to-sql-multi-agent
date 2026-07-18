"""Altair chart construction and the result-overview metric cards."""

from __future__ import annotations

import html
import re

import pandas as pd
import streamlit as st

# Neutral categorical palette, tuned for readability on a white background.
_OTP_CATEGORY_COLORS = ["#4F46E5", "#0EA5E9", "#059669", "#D97706", "#DC2626", "#7C3AED", "#0891B2", "#65A30D"]
_OTP_MUTED = "#6B7280"
_OTP_TEXT = "#111827"
_OTP_GRID = "#E5E7EB"

_altair_theme_registered = False


def _register_altair_theme() -> None:
    """Register (once) a custom Altair theme matching the app's light palette."""
    global _altair_theme_registered
    if _altair_theme_registered:
        return
    import altair as alt

    def _otp_theme():
        return {
            "config": {
                "background": "transparent",
                "view": {"stroke": "transparent"},
                "range": {"category": _OTP_CATEGORY_COLORS, "heatmap": _OTP_CATEGORY_COLORS},
                "axis": {
                    "domainColor": _OTP_GRID,
                    "gridColor": _OTP_GRID,
                    "tickColor": _OTP_GRID,
                    "labelColor": _OTP_MUTED,
                    "titleColor": _OTP_TEXT,
                    "labelFont": "Inter, sans-serif",
                    "titleFont": "Inter, sans-serif",
                },
                "legend": {
                    "labelColor": _OTP_MUTED,
                    "titleColor": _OTP_TEXT,
                    "labelFont": "Inter, sans-serif",
                    "titleFont": "Inter, sans-serif",
                },
                "mark": {"color": _OTP_CATEGORY_COLORS[0]},
                "bar": {"color": _OTP_CATEGORY_COLORS[0]},
                "line": {"color": _OTP_CATEGORY_COLORS[1], "strokeWidth": 3},
                "point": {"color": _OTP_CATEGORY_COLORS[1]},
                "arc": {"color": _OTP_CATEGORY_COLORS[0]},
            }
        }

    alt.themes.register("otp_neutral", _otp_theme)
    alt.themes.enable("otp_neutral")
    _altair_theme_registered = True


def build_custom_chart(df: pd.DataFrame, chart_type: str, x_col: str, y_col: str, color_col: str | None = None):
    """Create an interactive chart based on user choices and selected data."""
    import altair as alt
    _register_altair_theme()
    try:
        x_title = x_col.replace("_", " ").title()
        y_title = y_col.replace("_", " ").title()

        encodings = {
            "x": alt.X(f"{x_col}:N", title=x_title) if chart_type != "Line" and chart_type != "Area" else alt.X(f"{x_col}:O", title=x_title),
            "y": alt.Y(f"{y_col}:Q", title=y_title),
            "tooltip": list(df.columns),
        }

        if chart_type == "Bar" and (not color_col or color_col == "None"):
            encodings["x"] = alt.X(f"{x_col}:N", sort="-y", title=x_title)

        if color_col and color_col != "None":
            color_title = color_col.replace("_", " ").title()
            encodings["color"] = alt.Color(f"{color_col}:N", title=color_title)

        chart = alt.Chart(df)
        if chart_type == "Bar":
            return chart.mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(**encodings).properties(height=360)
        elif chart_type == "Line":
            if "date" in x_col.lower() or "week" in x_col.lower() or "month" in x_col.lower():
                encodings["x"] = alt.X(f"{x_col}:T", title=x_title)
            else:
                encodings["x"] = alt.X(f"{x_col}:O", title=x_title)
            return chart.mark_line(point=True, strokeWidth=3).encode(**encodings).properties(height=360)
        elif chart_type == "Area":
            if "date" in x_col.lower() or "week" in x_col.lower() or "month" in x_col.lower():
                encodings["x"] = alt.X(f"{x_col}:T", title=x_title)
            else:
                encodings["x"] = alt.X(f"{x_col}:O", title=x_title)
            return chart.mark_area(opacity=0.6).encode(**encodings).properties(height=360)
        elif chart_type == "Scatter":
            return chart.mark_circle(size=80).encode(**encodings).properties(height=360)
        elif chart_type == "Pie":
            pie_encodings = {
                "theta": alt.Theta(f"{y_col}:Q"),
                "color": alt.Color(f"{x_col}:N", title=x_title),
                "tooltip": list(df.columns),
            }
            return chart.mark_arc(outerRadius=120).encode(**pie_encodings).properties(height=360)
        return None
    except Exception:
        return None


def render_result_overview(df: pd.DataFrame) -> None:
    """Surface useful result context before users inspect the detailed data."""
    numeric = list(df.select_dtypes(include="number").columns)
    dimensions = [column for column in df.columns if column not in numeric]
    cards = st.columns(4)
    with cards[0]:
        render_metric_card("Result Rows", f"{len(df):,}")
    with cards[1]:
        render_metric_card("Breakdowns", f"{len(dimensions)}")
    if numeric:
        metric = numeric[-1]
        with cards[2]:
            render_metric_card(f"Avg {metric.replace('_', ' ').title()}", f"{df[metric].mean():,.2f}")
        with cards[3]:
            render_metric_card(f"Peak {metric.replace('_', ' ').title()}", f"{df[metric].max():,.2f}")
    else:
        with cards[2]:
            render_metric_card("Fields Returned", str(len(df.columns)))
        with cards[3]:
            render_metric_card("Analysis Status", "Ready")


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def inline_markdown_to_html(text: str) -> str:
    """Escape ``text`` for HTML, then honour ``**bold**`` spans.

    The metric cards and the comparison callout are injected as raw HTML
    (``unsafe_allow_html``) rather than parsed as markdown, so the synthesis
    agent's ``**...**`` would otherwise render as literal asterisks. Escaping
    before substituting also keeps model-written text -- and column aliases
    lifted from generated SQL -- from injecting markup of their own.
    """
    return _BOLD_RE.sub(lambda match: f"<strong>{match.group(1)}</strong>", html.escape(text))


def render_metric_card(label: str, value: str) -> None:
    """Render a themed KPI card (used on the landing dashboard and result overview)."""
    st.markdown(
        f"""
        <div class="otp-card">
            <div class="otp-kpi-label">{inline_markdown_to_html(label)}</div>
            <div class="otp-kpi-value">{inline_markdown_to_html(value)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
