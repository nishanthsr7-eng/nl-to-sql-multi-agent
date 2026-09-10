"""``sqe`` -- the command-line surface over :class:`AnalyticsPipeline`.

Design rules this module holds to, because breaking either of them is how a CLI
and its machine-readable output start disagreeing:

1. **One payload.** Every command that produces a result gets ``to_dict()`` from
   the pipeline and then either prints it as JSON or hands it to
   :mod:`semantic_query_engine.cli.render`. There is no path that reads the
   dataclass directly.
2. **``--json`` owns stdout.** Under ``--json`` nothing but the JSON document is
   written to stdout -- banners, warnings and progress go to stderr -- so
   ``sqe ask ... --json | jq .`` is always valid. Application logging already
   goes to stderr (see ``core.logging``), which is what makes this cheap.

Exit codes are the third part of the contract; see :mod:`.exit_codes`.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from semantic_query_engine.cli.exit_codes import ExitCode, exit_code_for
from semantic_query_engine.cli.render import render
from semantic_query_engine.core.catalog import example_questions
from semantic_query_engine.core.config import PROJECT_ROOT
from semantic_query_engine.core.domains import (
    DomainError,
    active_domain,
    available_domains,
    set_active_domain,
)
from semantic_query_engine.core.errors import WarehouseError
from semantic_query_engine.core.serialization import json_default
from semantic_query_engine.governance.audit import audit_log
from semantic_query_engine.governance.principals import (
    PrincipalError,
    load_principals,
    resolve_principal,
)
from semantic_query_engine.semantic.layer import load_semantic_layer

app = typer.Typer(
    name="sqe",
    help="Governed natural-language analytics over a declared warehouse domain.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _select_domain(
    domain: str = typer.Option(
        "",
        "--domain",
        help="Which warehouse to serve. See `sqe domains`. Default: SQE_DOMAIN, else retail.",
    ),
) -> None:
    """Resolve the domain before any command builds an agent.

    It has to happen here rather than inside each command because the agents
    capture the semantic layer, the registries and the allowed-table set when
    they are *constructed* -- the same reason ``LLMSettings`` is resolved at
    construction. A domain switched after that point would give an agent one
    domain's vocabulary over another domain's warehouse.
    """
    if not domain:
        return
    try:
        set_active_domain(domain)
    except DomainError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(int(ExitCode.USAGE)) from exc


def _force_utf8(stream: Any) -> None:
    """Make a legacy-codepage console able to print the narrative.

    A Windows terminal still defaults to cp1252, and synthesis output routinely
    contains ``£``, ``≈`` and en-dashes -- so the human renderer died with a
    ``UnicodeEncodeError`` on the exact machine this project is developed on,
    while ``--json`` survived only because ``json.dumps`` escapes non-ASCII.
    Re-encoding as UTF-8 with ``errors="replace"`` means the worst case is a
    substituted glyph rather than a crashed command.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    if (getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):  # pragma: no cover - already-detached stream
        pass


_force_utf8(sys.stdout)
_force_utf8(sys.stderr)

# stdout is reserved for results; everything else is diagnostics. See rule 2.
out = Console()
err = Console(stderr=True)


def _ensure_evals_importable() -> None:
    """Put the project root on ``sys.path`` so ``import evals`` resolves.

    ``evals/`` lives at the repo root rather than under ``src/``: it is the
    measurement harness for this checkout, not a library anyone installs. That
    makes it importable when the cwd is the repo root (``python -m pytest``,
    ``python -m semantic_query_engine.cli.main``) and *not* importable from the
    ``sqe`` console script, which does not put the cwd on the path -- so every
    ``sqe eval`` / ``sqe bench`` / ``sqe judge`` invocation through the installed
    entry point died on ``ModuleNotFoundError: No module named 'evals'``,
    including inside the container image. Anchoring on ``PROJECT_ROOT`` keeps
    this consistent with how the data and warehouse directories already resolve.
    """
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _emit(payload: dict[str, Any], as_json: bool, question: str, explain: bool) -> ExitCode:
    """Write one result in the requested format and return its exit code."""
    if as_json:
        out.file.write(json.dumps(payload, indent=2, default=json_default) + "\n")
        out.file.flush()
    else:
        render(out, payload, question=question, explain=explain)
    return exit_code_for(payload)


def _resolve_principal_or_exit(name: str) -> Any:
    """Turn ``--as`` into a principal, or exit 3.

    An unknown principal is a usage error rather than a query failure: nothing
    was asked of the warehouse, and resolving a typo to an anonymous default
    would quietly hand somebody access they were not given.
    """
    try:
        return resolve_principal(name or None)
    except PrincipalError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(int(ExitCode.USAGE)) from exc


def _build_pipeline(cache: bool = False) -> Any:
    """Construct the pipeline, turning warehouse problems into exit code 3.

    Imported lazily: ``AnalyticsPipeline.__init__`` opens DuckDB and builds the
    warehouse on first use, and ``sqe --help`` or ``sqe schema`` has no business
    paying for that.
    """
    from semantic_query_engine.core.semantic_cache import build_semantic_cache
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    try:
        return AnalyticsPipeline(cache=build_semantic_cache() if cache else None)
    except WarehouseError as exc:
        err.print(f"[red]Warehouse unavailable:[/red] {exc}")
        raise typer.Exit(int(ExitCode.USAGE)) from exc


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to answer, in plain English."),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw result payload to stdout and nothing else."),
    explain: bool = typer.Option(False, "--explain", help="Also show the SQL, the agent trace and timing."),
    cache: bool = typer.Option(
        False, "--cache", help="Reuse a previous question's SQL on a near-hit. See `sqe cache`."
    ),
    as_principal: str = typer.Option(
        "", "--as", help="Run as this principal, applying its row policies and PII access. See `sqe principals`."
    ),
) -> None:
    """Answer one question. Exit code: 0 answered, 2 clarification needed, 1 failed."""
    principal = _resolve_principal_or_exit(as_principal)
    pipeline = _build_pipeline(cache=cache)
    result = pipeline.run(question, principal=principal)
    # Persisted after the answer, not during it: a cache write is a side effect
    # of a question that succeeded, and a question that crashed the process
    # should not leave its SQL behind.
    if pipeline.cache is not None:
        pipeline.cache.save()
    raise typer.Exit(int(_emit(result.to_dict(), as_json, question, explain)))


@app.command()
def repl(
    explain: bool = typer.Option(False, "--explain", help="Show SQL, trace and timing for every turn."),
    cache: bool = typer.Option(
        False, "--cache", help="Reuse a previous question's SQL on a near-hit."
    ),
    as_principal: str = typer.Option("", "--as", help="Run every turn as this principal."),
) -> None:
    """Multi-turn session; an answer to a clarification is read against the question that prompted it.

    The pipeline already accepts prior turns (``run(question, context=[...])``)
    and consults them only when the current question is ambiguous on its own, so
    the REPL only has to keep the transcript -- there is no separate
    conversational state machine here to drift out of sync with the pipeline.
    """
    principal = _resolve_principal_or_exit(as_principal)
    pipeline = _build_pipeline(cache=cache)
    context: list[str] = []

    err.print("[bold]sqe repl[/bold] -- ask a question, or Ctrl-D to leave.")
    for label, example in example_questions()[:3]:
        err.print(f"  [dim]{label}:[/dim] {example}")

    while True:
        try:
            question = typer.prompt("\nsqe", prompt_suffix=" > ").strip()
        except (EOFError, KeyboardInterrupt, typer.Abort):
            err.print("\nbye")
            raise typer.Exit(int(ExitCode.ANSWER)) from None
        if not question:
            continue
        if question in {"exit", "quit"}:
            raise typer.Exit(int(ExitCode.ANSWER))

        result = pipeline.run(question, context=context, principal=principal)
        render(out, result.to_dict(), question=question, explain=explain)
        context.append(question)


@app.command()
def schema(
    table: str = typer.Option("", "--table", "-t", help="Show columns for one table instead of the summary."),
) -> None:
    """Browse the tables the semantic layer exposes."""
    layer = load_semantic_layer()
    if table:
        match = next(
            (t for t in layer.tables if str(t.get("table_name", "")).lower() == table.lower()), None
        )
        if match is None:
            err.print(f"[red]No such table:[/red] {table}")
            raise typer.Exit(int(ExitCode.USAGE))
        out.print(f"[bold]{match.get('table_name')}[/bold] -- {match.get('description', '')}\n")
        columns = Table(show_header=True, header_style="bold", box=None)
        columns.add_column("column")
        columns.add_column("type")
        columns.add_column("description", overflow="fold")
        for column in match.get("columns", []):
            columns.add_row(
                str(column.get("name", "")),
                str(column.get("type", "")),
                str(column.get("description", "")),
            )
        out.print(columns)
        return

    summary = Table(show_header=True, header_style="bold", box=None)
    summary.add_column("table")
    summary.add_column("columns", justify="right")
    summary.add_column("description", overflow="fold")
    for entry in layer.tables:
        summary.add_row(
            str(entry.get("table_name", "")),
            str(len(entry.get("columns", []))),
            str(entry.get("description", "")),
        )
    out.print(summary)


@app.command()
def metrics(
    search: str = typer.Option("", "--search", "-s", help="Filter by substring on name or description."),
) -> None:
    """Show the certified metric definitions -- name, formula, description."""
    layer = load_semantic_layer()
    needle = search.lower()
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("metric")
    table.add_column("definition", overflow="fold")
    table.add_column("description", overflow="fold")
    shown = 0
    for metric in layer.metrics:
        if needle and needle not in json.dumps(metric).lower():
            continue
        shown += 1
        table.add_row(
            str(metric.get("metric_name", "")),
            str(metric.get("definition", "")),
            str(metric.get("description", "")),
        )
    if not shown:
        err.print(f"[yellow]No metric matched[/yellow] {search!r}")
        raise typer.Exit(int(ExitCode.USAGE))
    out.print(table)


@app.command()
def cache(
    clear: bool = typer.Option(False, "--clear", help="Delete every entry for this domain."),
    as_json: bool = typer.Option(False, "--json", help="Emit the stats as JSON."),
) -> None:
    """Inspect or clear this domain's semantic cache.

    The cache is per domain and off by default -- ``sqe ask --cache`` opts in,
    and ``sqe eval --cache`` measures it. A cache that were on by default would
    change what every evaluation measures without anyone choosing that.
    """
    from semantic_query_engine.core.semantic_cache import build_semantic_cache

    store = build_semantic_cache()
    if clear:
        removed = store.clear()
        err.print(f"Cleared {removed} cached quer{'y' if removed == 1 else 'ies'}.")
        return

    domain = active_domain()
    if as_json:
        payload = {
            "domain": domain.name,
            "path": str(store.path),
            "entries": len(store),
            **store.stats.to_dict(),
        }
        out.file.write(json.dumps(payload, indent=2) + "\n")
        return

    out.print(f"[bold]{domain.name}[/bold] semantic cache -- {len(store)} entr"
              f"{'y' if len(store) == 1 else 'ies'} at {store.path}")
    if not len(store):
        out.print("[dim]Empty. Run `sqe ask --cache \"...\"` to populate it.[/dim]")


@app.command()
def principals() -> None:
    """List the identities this domain declares, and what each may see."""
    domain = active_domain()
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("principal")
    table.add_column("role")
    table.add_column("rows")
    table.add_column("pii")
    table.add_column("description", overflow="fold")
    for entry in load_principals(domain=domain).all():
        if entry.unrestricted:
            scope = "[yellow]all (unrestricted)[/yellow]"
        elif not entry.grants:
            scope = "all (no policy applies)"
        else:
            scope = "; ".join(
                f"{key}={', '.join(values) or '[red]none[/red]'}"
                for key, values in sorted(entry.grants.items())
            )
        table.add_row(entry.id, entry.role, scope, entry.pii_access, entry.description)
    out.print(table)
    err.print(
        f"\n[dim]{domain.name}: {domain.principals_path}. "
        "`sqe ask --as <principal>` runs one question as that identity.[/dim]"
    )


@app.command()
def audit(
    limit: int = typer.Option(20, "--limit", "-n", help="How many of the most recent records to show."),
    verify: bool = typer.Option(False, "--verify", help="Check the hash chain and report any break."),
    as_json: bool = typer.Option(False, "--json", help="Emit the records as JSON."),
) -> None:
    """Read this domain's append-only governance log.

    ``--verify`` walks the hash chain. It exits 1 on a break, so it is usable as
    a CI or cron check rather than only as something a human reads -- which is
    the difference between an audit trail and a log file.
    """
    log = audit_log()

    if verify:
        problems = log.verify()
        if problems:
            err.print(f"[red]Audit chain broken ({len(problems)} problem(s)):[/red]")
            for problem in problems:
                err.print(f"  {problem}")
            raise typer.Exit(int(ExitCode.FAILURE))
        err.print(f"[green]Audit chain intact[/green] -- {len(log.read())} record(s) at {log.path}")
        return

    records = log.read(limit=limit)
    if as_json:
        out.file.write(json.dumps(records, indent=2, default=json_default) + "\n")
        return
    if not records:
        err.print(f"[dim]No audit records yet at {log.path}.[/dim]")
        return

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("when")
    table.add_column("principal")
    table.add_column("outcome")
    table.add_column("rows", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("question", overflow="fold")
    for record in records:
        outcome = str(record.get("outcome", "?"))
        colour = {"answer": "green", "clarification": "yellow"}.get(outcome, "red")
        table.add_row(
            str(record.get("timestamp", ""))[:19],
            str(record.get("principal", "")) + (" *" if record.get("applied_policies") else ""),
            f"[{colour}]{outcome}[/{colour}]",
            str(record.get("row_count", "")),
            f"{float(record.get('elapsed_ms') or 0):.0f}",
            str(record.get("question", "")),
        )
    out.print(table)
    err.print("[dim]* row policies were applied to this run. `sqe audit --verify` checks the chain.[/dim]")


# Hoisted out of the signature because ruff's B008 forbids a call in an argument
# default, and typer's entire API is exactly that. A module-level singleton is
# typer's own recommended workaround and keeps the rule enabled for the rest of
# this file rather than silencing it with a per-line noqa.
_MODEL_OPTION = typer.Option(
    [],
    "--model",
    "-m",
    help="label=model-id to measure, repeatable. Default: the set in evals/model_matrix.py.",
)


@app.command()
def matrix(
    models: list[str] = _MODEL_OPTION,
    baseline: str = typer.Option("full", "--baseline", help="Which ladder rung every model runs."),
    limit: int = typer.Option(0, "--limit", help="Run only the first N gold cases (a smoke run)."),
    save: bool = typer.Option(True, "--save/--no-save", help="Write the report to evals/results/matrix/."),
    as_json: bool = typer.Option(False, "--json", help="Emit the report to stdout and nothing else."),
) -> None:
    """Run the gold suite across several models: accuracy vs cost vs latency.

    The ladder (``sqe eval``) holds the model fixed and varies the pipeline;
    this holds the pipeline fixed and varies the model. A model that cannot be
    reached, or whose run fell back to the deterministic templates, is reported
    as disqualified rather than scored -- see ``evals/model_matrix.py`` for why
    a footnote would not have been enough.
    """
    _ensure_evals_importable()
    from evals.harness import select_cases
    from evals.model_matrix import ModelSpec, default_models, run_matrix, save_matrix
    from evals.schema import load_gold_cases

    if models:
        specs = []
        for requested in models:
            label, _, model_id = requested.partition("=")
            if not model_id:
                err.print(f"[red]--model expects label=model-id, got[/red] {requested!r}")
                raise typer.Exit(int(ExitCode.USAGE))
            specs.append(ModelSpec(label=label, generator_model=model_id))
    else:
        specs = default_models()

    cases = select_cases(load_gold_cases(), limit=limit or None)
    if not cases:
        err.print("[red]No gold cases to run.[/red]")
        raise typer.Exit(int(ExitCode.USAGE))

    reachable = [spec for spec in specs if spec.is_reachable]
    err.print(
        f"[bold]matrix[/bold] -- {len(cases)} case(s) x {len(specs)} model(s) on the "
        f"'{baseline}' rung; {len(reachable)} reachable from this environment."
    )
    if not reachable:
        err.print(
            "[yellow]No API key is configured for any model, so every row would be the "
            "deterministic fallback rather than a measurement. Set SQE_LLM_API_KEY (and "
            "any per-model key) and re-run.[/yellow]"
        )

    def announce(entry: Any, _run: Any) -> None:
        err.print(
            f"  {entry.label}: {entry.execution_accuracy:.1f}% exec, "
            f"{entry.latency_p50_ms:.0f}ms p50"
            + (f" -- [red]{entry.disqualified}[/red]" if entry.disqualified else "")
        )

    report = run_matrix(cases, specs, baseline=baseline, on_model=announce)

    if as_json:
        out.file.write(json.dumps(report.to_dict(), indent=2, default=json_default) + "\n")
    else:
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("model")
        table.add_column("exec %", justify="right")
        table.add_column("value %", justify="right")
        table.add_column("rejected %", justify="right")
        table.add_column("p50 ms", justify="right")
        table.add_column("p95 ms", justify="right")
        table.add_column("$/query", justify="right")
        table.add_column("note", overflow="fold")
        for entry in report.entries:
            cost = (
                f"${entry.cost_per_query_usd:.6f}"
                if entry.cost_per_query_usd is not None
                else "n/a"
            )
            note = entry.disqualified or entry.note
            style = "red" if entry.disqualified else ""
            table.add_row(
                f"[{style}]{entry.label}[/{style}]" if style else entry.label,
                f"{entry.execution_accuracy:.1f}",
                f"{entry.value_accuracy:.1f}",
                f"{entry.first_attempt_rejection_rate:.1f}",
                f"{entry.latency_p50_ms:.0f}",
                f"{entry.latency_p95_ms:.0f}",
                cost,
                note,
            )
        out.print(table)
        if report.recommendation:
            out.print(f"\n[bold]Recommendation:[/bold] {report.recommendation}")
            out.print(f"[dim]{report.reasoning}[/dim]")
        else:
            out.print(f"\n[yellow]No recommendation.[/yellow] [dim]{report.reasoning}[/dim]")
        for caveat in report.caveats:
            err.print(f"[dim]  · {caveat}[/dim]")

    if save and report.comparable_entries:
        path = save_matrix(report)
        err.print(f"[dim]written to {path}[/dim]")
    elif save:
        # Nothing comparable means nothing to compare. Writing the file anyway
        # would put a document that looks like a measurement into the directory
        # where the measurements live.
        err.print("[yellow]Not saved: no model produced a comparable run.[/yellow]")


@app.command()
def domains() -> None:
    """List the warehouses this checkout can serve, and which one is active."""
    current = active_domain().name
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("domain")
    table.add_column("tables", justify="right")
    table.add_column("description", overflow="fold")
    for entry in available_domains():
        marker = " [green]*[/green]" if entry.name == current else ""
        table.add_row(
            f"{entry.name}{marker}",
            str(len(entry.tables)),
            entry.description or entry.title,
        )
    out.print(table)
    out.print("\n[dim]* active. Select another with `sqe --domain <name> ...`.[/dim]")


@app.command()
def examples() -> None:
    """Print the sample questions, one per question family."""
    for label, question in example_questions():
        out.print(f"[bold]{label}[/bold]\n  {question}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address. The container image overrides this to 0.0.0.0."),
    port: int = typer.Option(8000, help="Port to listen on."),
    reload: bool = typer.Option(False, "--reload", help="Reload on source change (development only)."),
) -> None:
    """Run the HTTP API over the same pipeline the CLI uses."""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - depends on install extras
        err.print("[red]uvicorn is not installed.[/red] Install the api extra: pip install -e .[api]")
        raise typer.Exit(int(ExitCode.USAGE)) from exc
    uvicorn.run("semantic_query_engine.api.app:app", host=host, port=port, reload=reload)


def _case_ticker(total: int) -> Any:
    """Build a per-case progress callback for a suite run, printing on stderr.

    A ladder run is hours long, and a counter that moves every tenth case says
    only that the process is alive. What a watcher actually needs is whether to
    let it finish: the running accuracy answers that, and the FALLBACK marker
    answers the more urgent version of it -- a provider that has started refusing
    sends every generation to the deterministic templates, and the run will
    complete looking fine and measuring nothing. Seeing that on case 12 is worth
    two hours.

    The running accuracy excludes adversarial cases so it agrees with the final
    report's denominator (``report.accuracy_cases``); a refusal suite mixed into
    the numerator would move the number according to how many injection probes
    the suite happens to contain.

    A factory rather than an inline closure because ``sqe bench`` repeats the
    same suite N times and needs the counter reset per repeat. Two copies of this
    display is how two commands start reporting progress differently.
    """
    state: dict[str, float] = {"n": 0, "scored": 0, "correct": 0, "start": time.perf_counter()}

    def tick(record: Any) -> None:
        state["n"] += 1
        fell_back = bool(record.sql_source) and not record.used_llm

        if record.adversarial:
            # Scored separately as a refusal rate, so it is shown but not folded
            # into the accuracy being tracked live.
            mark = "[cyan]guard[/cyan]" if record.executed else "[red]leak [/red]"
        else:
            state["scored"] += 1
            if record.values_correct:
                state["correct"] += 1
                mark = "[green]ok   [/green]"
            elif record.kind == "error":
                mark = "[red]err  [/red]"
            elif record.executed:
                # Ran, right shape, wrong numbers: the confidently-wrong case.
                mark = "[yellow]wrong[/yellow]"
            else:
                mark = "[red]fail [/red]"

        elapsed = time.perf_counter() - state["start"]
        eta_min = (elapsed / state["n"]) * (total - state["n"]) / 60
        accuracy = (state["correct"] / state["scored"] * 100) if state["scored"] else 0.0
        # Kept under 80 columns on purpose: a wrapped progress line turns a
        # scannable column of results into a wall of text, which is the one thing
        # this output exists to avoid.
        err.print(
            f"  [dim]{int(state['n']):>3}/{total}[/dim] {mark} "
            f"{record.case_id[:26]:<26} "
            f"[dim]{record.latency_ms / 1000:>5.1f}s[/dim] "
            f"[bold]{accuracy:>5.1f}%[/bold] "
            f"[dim]{int(state['correct'])}/{int(state['scored'])}[/dim] "
            f"[dim]eta {eta_min:>3.0f}m[/dim]"
            + ("  [yellow]FALLBACK[/yellow]" if fell_back else "")
        )

    return tick


# --- Evaluation ------------------------------------------------------------
# ``evals`` is imported inside the command rather than at module scope: it pulls
# in the whole measurement layer, and paying for that on ``sqe ask`` -- the hot
# path and the only command most people run -- to serve a command run weekly is
# the wrong trade.

@app.command("eval")
def eval_(
    suite: str = typer.Option("gold", "--suite", help="Which case suite to run."),
    baseline: str = typer.Option(
        "full",
        "--baseline",
        help="Ablation rung: naive | semantic | validator | full, or 'ladder' for all four.",
    ),
    difficulty: str = typer.Option(None, "--difficulty", help="Restrict to easy | medium | hard."),
    archetype: str = typer.Option(None, "--archetype", help="Restrict to one planner archetype."),
    limit: int = typer.Option(None, "--limit", help="Run only the first N cases (a smoke run)."),
    use_cache: bool = typer.Option(
        False,
        "--cache",
        help="Reuse cached SQL on a near-hit, and report the hit rate and saving. "
             "The report says plainly that such a run is not a clean model measurement.",
    ),
    skip_adversarial: bool = typer.Option(
        False, "--skip-adversarial", help="Omit the injection and out-of-range suite."
    ),
    save: bool = typer.Option(True, "--save/--no-save", help="Write the run to evals/results/."),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw run records instead of a report."),
    fail_under: float = typer.Option(
        None,
        "--fail-under",
        help="Exit 1 if value accuracy falls below this percentage. For CI gating.",
    ),
) -> None:
    """Run an evaluation suite and report stratified accuracy and the validator funnel."""
    _ensure_evals_importable()
    from evals.harness import load_gold_cases, run_suite, save_run, select_cases
    from evals.report import load_gold_tags, overall, render_ladder, render_run, safety

    from semantic_query_engine.core.config import ABLATION_LADDER

    if suite != "gold":
        err.print(f"[red]Unknown suite {suite!r}.[/red] Only 'gold' exists today.")
        raise typer.Exit(int(ExitCode.USAGE))

    rungs = list(ABLATION_LADDER) if baseline == "ladder" else [baseline]
    unknown = [rung for rung in rungs if rung not in ABLATION_LADDER]
    if unknown:
        err.print(f"[red]Unknown baseline(s) {unknown}.[/red] Choose from: {list(ABLATION_LADDER)}, ladder")
        raise typer.Exit(int(ExitCode.USAGE))

    try:
        cases = select_cases(
            load_gold_cases(),
            include_adversarial=not skip_adversarial,
            difficulty=difficulty,
            archetype=archetype,
            limit=limit,
        )
    except Exception as exc:
        err.print(f"[red]Could not load the gold set:[/red] {exc}")
        raise typer.Exit(int(ExitCode.USAGE)) from exc

    if not cases:
        err.print("[red]No cases matched those filters.[/red]")
        raise typer.Exit(int(ExitCode.USAGE))

    runs = []
    for rung in rungs:
        err.print(f"\n[bold]{rung}[/bold] -- {len(cases)} cases")
        tick = _case_ticker(len(cases))
        run = run_suite(cases, baseline=rung, suite=suite, on_case=tick, use_cache=use_cache)
        runs.append(run)
        if run.degraded:
            # Printed, not raised: the report is still worth seeing, and the
            # banner on it says what it is. But a degraded run must not be
            # written to evals/results/, because that directory is committed and
            # is the accuracy timeline -- a rate-limited afternoon would
            # otherwise enter git history as a genuine regression.
            err.print(
                f"[yellow]Run degraded:[/yellow] {run.fallback_share:.0%} of generations fell back "
                f"to the deterministic templates (provider unavailable or rate-limited). "
                f"{'Not saving.' if save else ''}"
            )
        elif save:
            err.print(f"[dim]Saved {save_run(run)}[/dim]")

    if as_json:
        out.file.write(json.dumps([run.to_dict() for run in runs], indent=2, default=json_default) + "\n")
    else:
        for run in runs:
            out.print(render_run(run))
            out.print("")
        if len(runs) > 1:
            out.print("## Baseline ladder\n")
            out.print(render_ladder(runs))

    # Containment is not an accuracy metric and does not wait for --fail-under.
    # Every other number here is a measurement that can legitimately move; a
    # mutation or an out-of-warehouse read that actually executed is a defect,
    # and a suite that prints one in a table and exits 0 has buried it. Checked
    # on every rung, including the un-validated ones, because those are exactly
    # where a breach would appear first.
    breached = [
        (run.baseline, outcome)
        for run in runs
        for outcome in safety(run, load_gold_tags()).breaches
    ]
    if breached:
        for baseline, outcome in breached:
            err.print(
                f"[red]Containment breach[/red] on the {baseline} rung: "
                f"{outcome.case_id} executed {outcome.breach_reason}."
            )
        raise typer.Exit(int(ExitCode.FAILURE))

    # A row-policy breach is the same class of event as a containment breach --
    # something that should have been impossible has already happened -- so it
    # gets the same treatment: no rate, no gate flag, straight to a non-zero
    # exit. It can only fire on a suite that contains restricted-principal
    # cases; a run made entirely of steward cases says nothing here and is not
    # cleared by it either.
    scoped_breaches = [
        (run.baseline, record) for run in runs for record in run.row_policy_breaches
    ]
    if scoped_breaches:
        for baseline, record in scoped_breaches:
            err.print(
                f"[red]Row policy breach[/red] on the {baseline} rung: "
                f"{record.case_id} ran as {record.principal} and "
                + "; ".join(record.row_policy_breaches)
                + "."
            )
        raise typer.Exit(int(ExitCode.FAILURE))

    # A PII leak is the same class of event as a row-policy or containment
    # breach -- personal data reached a caller not cleared to see it -- so it
    # gets the same treatment: no rate, no gate flag, straight to a non-zero
    # exit. It can only fire on a suite containing masked-principal cases.
    leaked = [(run.baseline, record) for run in runs for record in run.pii_leaks]
    if leaked:
        for baseline, record in leaked:
            err.print(
                f"[red]PII leak[/red] on the {baseline} rung: "
                f"{record.case_id} ran as {record.principal} and "
                + "; ".join(record.pii_leaks)
                + "."
            )
        raise typer.Exit(int(ExitCode.FAILURE))

    # The gate is evaluated on the *last* rung, which is the full pipeline when
    # running the ladder -- that is the configuration the project ships, so it is
    # the one a regression should block on.
    if fail_under is not None:
        # A degraded run cannot clear or fail an accuracy gate honestly: its
        # number describes the fallback. Exit 3 (the engine could not run the
        # measurement) rather than 1 (accuracy regressed), so CI reports an
        # infrastructure problem instead of blaming the model.
        if runs[-1].degraded:
            err.print(
                "[red]Cannot evaluate the gate:[/red] the run was degraded "
                f"({runs[-1].fallback_share:.0%} fallback). This is a provider problem, "
                "not an accuracy regression."
            )
            raise typer.Exit(int(ExitCode.USAGE))
        achieved = overall(runs[-1]).value_accuracy * 100
        if achieved < fail_under:
            err.print(
                f"[red]Value accuracy {achieved:.1f}% is below the {fail_under:.1f}% gate.[/red]"
            )
            raise typer.Exit(int(ExitCode.FAILURE))


@app.command()
def bench(
    repeats: int = typer.Option(3, "--runs", help="Repeats of the suite, for the spread at temperature 0."),
    suite: str = typer.Option("gold", "--suite", help="Which case suite to repeat."),
    baseline: str = typer.Option(
        "full", "--baseline", help="Ablation rung to repeat: naive | semantic | validator | full."
    ),
    limit: int = typer.Option(None, "--limit", help="Run only the first N cases (a smoke run)."),
    save: bool = typer.Option(True, "--save/--no-save", help="Write each repeat to evals/results/."),
    from_results: bool = typer.Option(
        False,
        "--from-results",
        help="Re-report the last N committed runs of this baseline instead of running the suite.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the variance report as JSON."),
    fail_over: float = typer.Option(
        None,
        "--fail-over",
        help="Exit 1 if the flip rate exceeds this percentage. For gating on instability.",
    ),
) -> None:
    """Repeat a suite and report run-to-run variance (ROADMAP Phase 3 task 7).

    Temperature 0 is a greedy decode, not a deterministic one, so every accuracy
    this project publishes is one sample from an unmeasured distribution. This
    command measures it, and reports two things rather than one: the spread of
    the headline number, and the share of individual cases that changed verdict
    underneath a headline that did not move. The second is usually the larger.
    """
    _ensure_evals_importable()
    from evals.harness import (
        RESULTS_DIR,
        load_gold_cases,
        load_run,
        run_suite,
        save_run,
        select_cases,
    )
    from evals.variance import VarianceError, render_variance, variance

    from semantic_query_engine.core.config import ABLATION_LADDER, load_llm_settings

    if suite != "gold":
        err.print(f"[red]Unknown suite {suite!r}.[/red] Only 'gold' exists today.")
        raise typer.Exit(int(ExitCode.USAGE))
    if baseline not in ABLATION_LADDER:
        err.print(f"[red]Unknown baseline {baseline!r}.[/red] Choose from: {list(ABLATION_LADDER)}")
        raise typer.Exit(int(ExitCode.USAGE))
    if repeats < 2:
        err.print("[red]--runs must be at least 2[/red]; variance across one run is not a thing.")
        raise typer.Exit(int(ExitCode.USAGE))

    runs = []
    if from_results:
        # Re-scoring committed artefacts is the cheap path and the one that gets
        # used: an overnight 3x suite is recorded once and re-reported whenever
        # the variance metrics change, without paying for the runs again.
        # Filtered by domain before the tail is taken, not after: the artefacts
        # share one directory and one filename shape, so the newest N of a rung
        # can straddle two warehouses. Taking the tail first made `bench
        # --from-results` fail on a mixed set that variance would rightly refuse
        # to compare -- the runs were there, they were just the wrong ones.
        domain = active_domain().name
        candidates = sorted(RESULTS_DIR.glob(f"*_{suite}_{baseline}.json"))
        runs = [run for run in (load_run(path) for path in candidates) if run.domain == domain]
        runs = runs[-repeats:]
        if len(runs) < 2:
            err.print(
                f"[red]Found {len(runs)} committed run(s) for {baseline!r} on "
                f"{domain!r}[/red] -- need at least 2. Drop --from-results to run "
                "the suite."
            )
            raise typer.Exit(int(ExitCode.USAGE))
        err.print(f"[dim]Re-scoring {len(runs)} committed run(s) of {baseline}.[/dim]")
    else:
        # Checked before the first repeat rather than after the third: with no
        # provider every generation comes from the deterministic templates, which
        # answer identically every time. The run would complete, report a 0% flip
        # rate, and mean nothing -- after an hour.
        if load_llm_settings().provider == "none":
            err.print(
                "[red]No LLM provider configured.[/red] Every generation would come from "
                "the deterministic templates, which are identical every run -- the report "
                "would show perfect stability and measure nothing."
            )
            raise typer.Exit(int(ExitCode.USAGE))

        try:
            cases = select_cases(load_gold_cases(), limit=limit)
        except Exception as exc:
            err.print(f"[red]Could not load the gold set:[/red] {exc}")
            raise typer.Exit(int(ExitCode.USAGE)) from exc

        for index in range(repeats):
            err.print(f"\n[bold]{baseline} -- repeat {index + 1}/{repeats}[/bold] ({len(cases)} cases)")
            run = run_suite(cases, baseline=baseline, suite=suite, on_case=_case_ticker(len(cases)))
            if run.degraded:
                # Fatal here, unlike in `sqe eval`. A degraded rung still produces
                # a report worth reading; a degraded *repeat* poisons the whole
                # measurement, because the deterministic fallback answers
                # identically every time and would be reported as the model
                # having been perfectly stable.
                err.print(
                    f"[red]Repeat {index + 1} degraded[/red] ({run.fallback_share:.0%} fallback). "
                    "Fallback output is identical every run, so continuing would report "
                    "a stability the model did not earn. Stopping."
                )
                raise typer.Exit(int(ExitCode.USAGE))
            runs.append(run)
            if save and limit:
                # A truncated suite is a different denominator wearing the same
                # filename shape, and `latest_run()` -- which is what the
                # regression gate compares against -- cannot tell the two apart.
                # A smoke run that silently became the baseline would move the
                # gate by however many cases someone happened to pass.
                err.print("[dim]Not saved: --limit makes this a smoke run, not a measurement.[/dim]")
            elif save:
                err.print(f"[dim]Saved {save_run(run)}[/dim]")

    try:
        report = variance(runs)
    except VarianceError as exc:
        err.print(f"[red]Cannot compare these runs:[/red] {exc}")
        raise typer.Exit(int(ExitCode.USAGE)) from exc

    if as_json:
        out.file.write(json.dumps(report.to_dict(), indent=2, default=json_default) + "\n")
    else:
        out.print(render_variance(report))

    if fail_over is not None and report.flip_rate * 100 > fail_over:
        err.print(
            f"[red]Flip rate {report.flip_rate * 100:.1f}% exceeds the "
            f"{fail_over:.1f}% gate[/red] -- {len(report.flipped)} of {report.scored_cases} "
            "cases did not score the same way every run."
        )
        raise typer.Exit(int(ExitCode.FAILURE))


@app.command()
def judge(
    from_results: str = typer.Option(
        None,
        "--from-results",
        help="Judge narratives from a recorded run. Defaults to the latest run of --baseline.",
    ),
    baseline: str = typer.Option("full", "--baseline", help="Which rung's latest run to judge."),
    limit: int = typer.Option(None, "--limit", help="Judge only the first N answered cases."),
    label: bool = typer.Option(
        False, "--label", help="Hand-label a sample instead of judging: writes judge_labels.json."
    ),
    sample: int = typer.Option(20, "--sample", help="How many narratives to put up for labelling."),
    as_json: bool = typer.Option(False, "--json", help="Emit the judge report as JSON."),
) -> None:
    """Score the synthesis narrative (ROADMAP Phase 3 task 5).

    Two halves, deliberately separated. Grounding -- is every figure in the prose
    actually in the rows -- is arithmetic, so it is checked deterministically and
    runs with no provider at all. Only meaning goes to the judge: relevance,
    faithfulness and calibration. Handing the arithmetic to a model would put a
    second hallucinator in the seat where the first one's hallucinations are being
    counted.

    Narratives are read from a *recorded* run, so judging costs one pass over an
    artefact rather than re-running the suite.
    """
    _ensure_evals_importable()
    from evals.harness import RESULTS_DIR, latest_run, load_gold_cases, load_run
    from evals.judge import (
        JudgeReport,
        calibration,
        judge_model_for,
        judge_narrative,
        load_labels,
        render_judge,
        total_usage,
    )

    from semantic_query_engine.core.config import ABLATION_LADDER, load_llm_settings

    if from_results:
        path = pathlib.Path(from_results)
        if not path.exists():
            err.print(f"[red]No such run:[/red] {path}")
            raise typer.Exit(int(ExitCode.USAGE))
        run = load_run(path)
    else:
        if baseline not in ABLATION_LADDER:
            err.print(f"[red]Unknown baseline {baseline!r}.[/red] Choose from: {list(ABLATION_LADDER)}")
            raise typer.Exit(int(ExitCode.USAGE))
        found = latest_run(baseline, RESULTS_DIR)
        if found is None:
            err.print(
                f"[red]No recorded run for {baseline!r}.[/red] Run `sqe eval --baseline {baseline}` first."
            )
            raise typer.Exit(int(ExitCode.USAGE))
        run = found

    # Only answered cases have a narrative to score. A failure's message is not
    # prose written for a user, and folding it in would move the pass rate by how
    # often the pipeline declined -- which the accuracy suite already reports.
    answered = [
        record for record in run.records if record.kind == "answer" and record.narrative
    ]
    if not answered:
        err.print(
            "[red]That run carries no narratives.[/red] Artefacts recorded before the "
            "judge tier landed do not store them -- re-run `sqe eval` to record a run "
            "that does."
        )
        raise typer.Exit(int(ExitCode.USAGE))
    if limit:
        answered = answered[:limit]

    # The question lives on the gold case, not on the record -- and the judge
    # cannot score relevance without it. A case whose question has since been
    # renamed away is judged on faithfulness and calibration only rather than
    # against a placeholder, because a judge shown the wrong question would mark
    # a good narrative irrelevant.
    questions = {case.id: case.question for case in load_gold_cases()}

    if label:
        _label_narratives(answered[:sample], questions)
        return

    settings = load_llm_settings()
    if settings.provider == "none":
        err.print(
            "[yellow]No provider configured.[/yellow] Reporting the grounding half only, "
            "which needs no model."
        )

    verdicts = []
    for index, record in enumerate(answered, start=1):
        verdict = judge_narrative(
            record.case_id,
            questions.get(record.case_id, ""),
            record.narrative,
            record.result_sample,
            settings=settings,
        )
        verdicts.append(verdict)
        mark = (
            "[green]pass [/green]"
            if verdict.passes
            else ("[red]ungrd[/red]" if not verdict.grounding.grounded else "[yellow]weak [/yellow]")
        )
        err.print(f"  [dim]{index:>3}/{len(answered)}[/dim] {mark} {verdict.case_id[:40]}")

    report = JudgeReport(
        judge_model=judge_model_for(settings) if settings.provider != "none" else "",
        generator_model=run.generator_model,
        verdicts=verdicts,
    )
    report.agreement = calibration(verdicts, load_labels())

    if as_json:
        out.file.write(json.dumps(report.to_dict(), indent=2, default=json_default) + "\n")
    else:
        out.print(render_judge(report))
        err.print(f"[dim]Judge spend: {total_usage(report).total_tokens:,} tokens[/dim]")


def _label_narratives(records: list[Any], questions: dict[str, str]) -> None:
    """Hand-label a sample against the same rubric the judge uses.

    Writes to the same file the judge reads, so labelling and calibration cannot
    drift apart into two formats. Existing labels are kept and re-labelled cases
    overwritten, so a session can be stopped and resumed -- twenty narratives is
    more than most people will sit through in one go, and a flow that loses work
    on exit is a flow that produces no labels.
    """
    _ensure_evals_importable()
    from evals.judge import (
        AXES,
        LABELS_PATH,
        SYSTEM_PROMPT,
        build_user_prompt,
        load_labels,
    )

    existing = load_labels()
    todo = [record for record in records if record.case_id not in existing]
    if not todo:
        err.print(f"[green]All {len(records)} sampled narratives are already labelled.[/green]")
        return

    err.print(
        f"\n[bold]Labelling {len(todo)} narrative(s)[/bold] -- score 0, 1 or 2 per axis.\n"
        "[dim]Enter nothing to skip a case, or Ctrl-C to stop and keep what you have.[/dim]"
    )
    # The rubric is printed rather than assumed remembered, and it is the
    # judge's own SYSTEM_PROMPT for the same reason the prompt builder is
    # reused below: a human scoring against a paraphrase of the rubric is not
    # disagreeing with the judge, they are answering a different question.
    # Trimmed at the JSON instruction: everything above it is the rubric both
    # graders answer, and everything below is how the model is asked to reply,
    # which a human reading it would only find confusing.
    err.print(f"[dim]{SYSTEM_PROMPT.split(chr(34) + chr(34))[0].split('Reply with JSON')[0].rstrip()}[/dim]")

    raw = json.loads(LABELS_PATH.read_text(encoding="utf-8")) if LABELS_PATH.exists() else {}
    labels = {entry["case_id"]: entry for entry in raw.get("labels", [])}

    try:
        for index, record in enumerate(todo, start=1):
            err.print(f"\n[bold]{index}/{len(todo)}[/bold]  {record.case_id}")
            # Rendered with the judge's own prompt builder rather than a second,
            # prettier formatter. Calibration measures agreement between two
            # graders, which only means anything if both were shown the same
            # thing -- a human given four rows where the judge saw fifteen would
            # mark claims about the tail unfaithful, and the disagreement would
            # be the tooling rather than the judgement.
            err.print(build_user_prompt(
                questions.get(record.case_id, ""), record.narrative, record.result_sample
            ))
            entry: dict[str, Any] = {"case_id": record.case_id}
            skipped = False
            for axis in AXES:
                answer = typer.prompt(f"  {axis} (0/1/2)", default="", show_default=False)
                if answer.strip() == "":
                    skipped = True
                    break
                entry[axis] = max(0, min(2, int(answer.strip())))
            if skipped:
                continue
            entry["reason"] = typer.prompt("  reason (optional)", default="", show_default=False)
            labels[record.case_id] = entry
    except (KeyboardInterrupt, EOFError):
        err.print("\n[yellow]Stopped.[/yellow] Keeping the labels entered so far.")

    raw["labels"] = sorted(labels.values(), key=lambda entry: entry["case_id"])
    LABELS_PATH.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    err.print(f"[green]Wrote {len(raw['labels'])} label(s)[/green] to {LABELS_PATH}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
