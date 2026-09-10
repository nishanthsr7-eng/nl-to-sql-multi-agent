"""Human-readable renderers for a serialised pipeline result.

Every function here takes the ``dict`` produced by ``QueryResult.to_dict()`` --
never the dataclass. That is the whole point of the module: ``--json`` prints
that payload verbatim, and the terminal view is a *formatter over the same
payload*, so a field the renderer shows is by construction a field the JSON
contains. The previous UI read attributes off the dataclass directly, which let
the two surfaces drift until they disagreed about what a run had produced.

Rendering branches on ``payload["kind"]``, the serialised discriminant, for the
same reason.
"""

from __future__ import annotations

import math
from typing import Any

from rich.align import AlignMethod
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from semantic_query_engine.core.catalog import recovery_ideas
from semantic_query_engine.core.usage import format_cost

# A terminal is not a spreadsheet: past a couple of screens the table stops being
# readable and the useful move is "re-run with --json". The pipeline's own cap
# (PipelineSettings.max_result_rows) still governs what was fetched; this only
# governs what is printed.
DEFAULT_MAX_TABLE_ROWS = 30


def format_cell(value: Any) -> str:
    """Render one result-set value for a fixed-width terminal column.

    Floats are the only interesting case. DuckDB hands back full precision, and
    a column of ``2861234.5600000005`` makes a table unreadable while implying an
    accuracy the underlying data does not have -- so they are shown to two
    decimals, with integral floats shown as integers because ``2024.00`` for a
    year is actively misleading.

    Fractions below 1 are the exception and get four decimals: rates are the
    common case there (a stock-depletion rate of 0.1436), and two decimals
    collapsed a whole ranked column to ``0.14`` -- visibly identical rows in a
    table whose entire point was the ordering between them.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return "-"
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        if 0 < abs(value) < 1:
            return f"{value:.4f}"
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def build_result_table(rows: list[dict[str, Any]], max_rows: int = DEFAULT_MAX_TABLE_ROWS) -> Table:
    """A rich table over the result rows, with a footnote when rows are elided."""
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    columns = list(rows[0].keys()) if rows else []
    for column in columns:
        # Numeric columns read far better right-aligned, and the first row is a
        # good enough sample: DuckDB gives a column one type.
        numeric = isinstance(rows[0][column], (int, float)) and not isinstance(rows[0][column], bool)
        justify: AlignMethod = "right" if numeric else "left"
        table.add_column(column, justify=justify, overflow="fold")
    for row in rows[:max_rows]:
        table.add_row(*(format_cell(row[column]) for column in columns))
    if len(rows) > max_rows:
        table.caption = f"showing {max_rows} of {len(rows)} rows -- use --json for the full result set"
    return table


def _render_trace(console: Console, payload: dict[str, Any]) -> None:
    """The agent trace and timing, shown under ``--explain``."""
    trace = payload.get("agent_trace") or []
    if trace:
        console.print("\n[bold]Agent trace[/bold]")
        for step in trace:
            console.print(Text("  • ", style="dim").append(str(step), style="none"))
    _render_governance(console, payload.get("governance") or {})
    elapsed = payload.get("elapsed_ms")
    if elapsed is not None:
        source = payload.get("sql_source") or "n/a"
        console.print(f"\n[dim]elapsed {elapsed} ms · sql source: {source}[/dim]")
    stages = payload.get("stage_latency_ms") or {}
    if stages:
        # Slowest first: the only reason to read this is to find out what to
        # go and fix, and that is almost always the top line.
        ordered = sorted(stages.items(), key=lambda item: item[1], reverse=True)
        console.print(
            "[dim]  " + " · ".join(f"{name} {ms:.0f}ms" for name, ms in ordered) + "[/dim]"
        )
    _render_usage(console, payload.get("usage") or {})


def _render_governance(console: Console, governance: dict[str, Any]) -> None:
    """Who the run ran as, and what that cost them in rows and columns.

    Shown whenever anything was actually applied, not only under ``--explain``'s
    other sections: an answer computed over a fraction of the warehouse looks
    exactly like an answer computed over all of it, and the difference is the
    whole point of the feature.
    """
    policies = governance.get("applied_policies") or []
    masked = governance.get("masked_columns") or []
    if not policies and not masked:
        return

    console.print(f"\n[bold]Governance[/bold] [dim](as {governance.get('principal', '?')})[/dim]")
    for policy in policies:
        console.print(
            Text("  • ", style="dim")
            .append(f"{policy.get('policy', '')} on {policy.get('table', '')}: ", style="none")
            .append(str(policy.get("predicate", "")), style="dim")
        )
    for column in masked:
        console.print(
            Text("  • ", style="dim").append(
                f"{column.get('column', '')} masked "
                f"({column.get('classification', '')}, {column.get('strategy', '')})",
                style="none",
            )
        )


def _render_usage(console: Console, usage: dict[str, Any]) -> None:
    """Token spend for the run, shown under ``--explain``.

    Printed only when a provider call actually happened. A "0 tokens · $0.000000"
    line under a run the deterministic fallback answered reads like a measurement
    of a free model rather than the absence of a call; ``sql_source`` already says
    which path ran, so this line stays quiet when there is no spend to report.
    """
    if not usage.get("calls"):
        return
    console.print(
        f"[dim]tokens {usage.get('total_tokens', 0)} "
        f"({usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out) "
        f"in {usage['calls']} call(s) · cost {format_cost(usage.get('cost_usd'))}[/dim]"
    )


def _render_sql(console: Console, sql: str) -> None:
    if not sql:
        return
    console.print("\n[bold]SQL[/bold]")
    console.print(Syntax(sql, "sql", theme="ansi_dark", word_wrap=True))


def render_answer(console: Console, payload: dict[str, Any], explain: bool = False) -> None:
    key_metric = payload.get("key_metric")
    if key_metric:
        console.print(Panel(Text(str(key_metric), style="bold"), expand=False, border_style="green"))
    console.print(str(payload.get("narrative_summary", "")))

    comparison = payload.get("comparison_context")
    if comparison:
        console.print(f"[dim]{comparison}[/dim]")

    rows = payload.get("result_table") or []
    if rows:
        console.print()
        console.print(build_result_table(rows))

    if payload.get("truncated"):
        # The pipeline capped an unbounded query, so these rows are a prefix of
        # the answer, not the answer. Saying so is the difference between a
        # partial result and a wrong one.
        console.print("\n[yellow]Result set was capped by the validator -- these are the first rows, not the full answer.[/yellow]")

    chart = payload.get("chart_recommendation")
    if chart:
        console.print(f"\n[dim]suggested chart: {chart}[/dim]")

    if explain:
        archetype = payload.get("archetype_label") or ""
        intent = payload.get("intent") or ""
        console.print(f"\n[bold]Plan[/bold]  intent={intent or 'n/a'}  archetype={archetype or 'n/a'}")
        if payload.get("archetype_description"):
            console.print(f"[dim]{payload['archetype_description']}[/dim]")
        _render_sql(console, str(payload.get("sql_query", "")))
        _render_trace(console, payload)


def render_clarification(console: Console, payload: dict[str, Any], explain: bool = False) -> None:
    console.print(Panel(str(payload.get("prompt", "")), title="Clarification needed", border_style="yellow", expand=False))
    missing = payload.get("missing_params") or []
    if missing:
        console.print(f"[dim]missing: {', '.join(str(p) for p in missing)}[/dim]")
    if explain:
        _render_trace(console, payload)


def render_failure(console: Console, payload: dict[str, Any], question: str = "", explain: bool = False) -> None:
    reason = str(payload.get("reason", "unknown"))
    console.print(Panel(str(payload.get("message", "")), title=f"Failed ({reason})", border_style="red", expand=False))

    for detail in payload.get("details") or []:
        console.print(f"  [red]•[/red] {detail}")

    codes = payload.get("issue_codes") or []
    if codes:
        # The codes are the eval funnel's group-by key; showing them in the CLI
        # means a human debugging one query and a report aggregating a thousand
        # are looking at the same vocabulary.
        console.print(f"\n[dim]validator issues: {', '.join(str(c) for c in codes)}[/dim]")

    if question:
        console.print("\n[bold]Try instead[/bold]")
        for idea in recovery_ideas(question):
            console.print(f"  [dim]•[/dim] {idea}")

    if explain:
        _render_sql(console, str(payload.get("sql_query", "")))
        _render_trace(console, payload)


def render(console: Console, payload: dict[str, Any], question: str = "", explain: bool = False) -> None:
    """Render any pipeline result. Dispatches on the serialised ``kind``."""
    kind = payload.get("kind")
    if kind == "answer":
        render_answer(console, payload, explain=explain)
    elif kind == "clarification":
        render_clarification(console, payload, explain=explain)
    elif kind == "failure":
        render_failure(console, payload, question=question, explain=explain)
    else:  # pragma: no cover - only reachable if a new variant skips this module
        console.print(f"[red]Unrecognised result kind: {kind!r}[/red]")


__all__ = [
    "build_result_table",
    "format_cell",
    "render",
    "render_answer",
    "render_clarification",
    "render_failure",
]
