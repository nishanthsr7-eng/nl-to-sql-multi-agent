"""CLI contract tests: exit codes, stdout purity, and payload fidelity.

The CLI's value over a web page is that a script can consume it, and that rests
on three promises, each guarded below: the exit code tells you the outcome,
``--json`` puts nothing but JSON on stdout, and the JSON is exactly the
pipeline's own payload rather than a re-serialisation that can drift from it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from typer.testing import CliRunner

from semantic_query_engine.cli import main as cli_main
from semantic_query_engine.cli.render import build_result_table, format_cell
from semantic_query_engine.core.results import Clarification, Failure, StructuredResponse

runner = CliRunner()


class _FakePipeline:
    """Stands in for AnalyticsPipeline so no test here opens DuckDB or an LLM."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, list[str]]] = []
        # Who each call ran as, so a test can assert that ``--as`` reached the
        # pipeline rather than merely being accepted by the argument parser.
        self.principals: list[str] = []
        # The real pipeline exposes this; the CLI saves it after an answer.
        # None means the semantic cache was not asked for, which is the default.
        self.cache = None

    def run(self, question, context=None, principal=None):
        self.calls.append((question, list(context or [])))
        self.principals.append(principal.id if principal is not None else None)
        return self._result


def _answer(**overrides):
    payload = dict(
        narrative_summary="PL-South leads on revenue.",
        key_metric="£6.67M Total Revenue",
        comparison_context="PL-South edged PL-North by £2K.",
        chart_recommendation="bar",
        sql_query="SELECT region FROM fmcg_sales LIMIT 10",
        result_table=[{"region": "PL-South", "total_revenue": 6_666_229.81}],
        intent="descriptive_lookup",
        agent_trace=["Planner: ok", "Validator: passed"],
        sql_source="llm",
    )
    payload.update(overrides)
    return StructuredResponse(**payload)


@pytest.fixture
def install_pipeline(monkeypatch):
    """Patch the pipeline the CLI constructs, returning the fake it will get."""

    def _install(result):
        fake = _FakePipeline(result)
        monkeypatch.setattr(cli_main, "_build_pipeline", lambda cache=False: fake)
        return fake

    return _install


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (_answer(), 0),
        (Clarification(prompt="Which metric?", missing_params=["metric"]), 2),
        (Failure(reason="validation_failed", message="SQL validation failed", issue_codes=["UNKNOWN_COLUMN"]), 1),
        (Failure(reason="no_data", message="No data matched the request"), 1),
    ],
)
def test_ask_exit_code_distinguishes_the_three_outcomes(install_pipeline, result, expected_code):
    """A clarification must not exit 0: a script has to be able to tell an answer
    from "the engine needs more scope" without parsing prose."""
    install_pipeline(result)
    invocation = runner.invoke(cli_main.app, ["ask", "anything"])
    assert invocation.exit_code == expected_code


def test_json_mode_puts_nothing_but_json_on_stdout(install_pipeline):
    """Guards ``sqe ask --json | jq .``: a single stray banner line breaks every
    downstream consumer, and the human renderer is chatty by design."""
    install_pipeline(_answer())
    invocation = runner.invoke(cli_main.app, ["ask", "total revenue by region", "--json"])
    assert invocation.exit_code == 0
    parsed = json.loads(invocation.stdout)  # would raise if anything else were printed
    assert parsed["kind"] == "answer"


def test_json_output_is_the_pipeline_payload_verbatim(install_pipeline):
    """The JSON surface and the human surface must be the same payload. If the
    CLI ever builds its own dict, the two will disagree about what a run did."""
    result = _answer()
    install_pipeline(result)
    invocation = runner.invoke(cli_main.app, ["ask", "q", "--json"])
    assert json.loads(invocation.stdout) == json.loads(json.dumps(result.to_dict()))


def test_json_keeps_numpy_scalars_as_numbers(install_pipeline):
    """DuckDB hands back numpy scalars. ``default=str`` would emit "6666229.81"
    as a *string*, forcing every consumer -- the Phase 3 eval harness included --
    to parse numbers back out of JSON strings."""
    install_pipeline(_answer(result_table=[{"units": np.int64(42), "revenue": np.float64(6_666_229.81)}]))
    row = json.loads(runner.invoke(cli_main.app, ["ask", "q", "--json"]).stdout)["result_table"][0]
    assert row == {"units": 42, "revenue": 6_666_229.81}


def test_failure_output_names_the_validator_issue_codes(install_pipeline):
    """The codes are the eval funnel's group-by key; a human debugging one query
    should see the same vocabulary a report aggregating a thousand uses."""
    install_pipeline(
        Failure(reason="validation_failed", message="SQL validation failed", issue_codes=["FORBIDDEN_TABLE"])
    )
    invocation = runner.invoke(cli_main.app, ["ask", "drop everything"])
    assert "FORBIDDEN_TABLE" in invocation.stdout


def test_truncated_answer_is_flagged_to_the_user(install_pipeline):
    """A capped result set read as a complete one is a wrong answer, not a
    partial one -- the renderer has to say so."""
    install_pipeline(_answer(truncated=True))
    invocation = runner.invoke(cli_main.app, ["ask", "every row please"])
    assert "capped" in invocation.stdout.lower()


def test_explain_shows_sql_and_trace_and_plain_mode_does_not(install_pipeline):
    install_pipeline(_answer())
    plain = runner.invoke(cli_main.app, ["ask", "q"]).stdout
    explained = runner.invoke(cli_main.app, ["ask", "q", "--explain"]).stdout
    assert "Agent trace" not in plain
    assert "Agent trace" in explained
    assert "fmcg_sales" in explained


def test_schema_command_does_not_build_the_warehouse(monkeypatch):
    """``sqe schema`` reads the semantic layer only. If it ever constructs the
    pipeline it will rebuild DuckDB from CSVs just to print a table list."""

    def _explode():  # pragma: no cover - only runs if the guard is broken
        raise AssertionError("sqe schema must not construct the pipeline")

    monkeypatch.setattr(cli_main, "_build_pipeline", _explode)
    invocation = runner.invoke(cli_main.app, ["schema"])
    assert invocation.exit_code == 0
    assert "fmcg_sales" in invocation.stdout


def test_unknown_table_exits_usage_not_failure():
    """Exit 3 separates "you asked for something that isn't there" from "the
    model produced bad SQL" (1). Collapsing them makes the CLI unscriptable."""
    invocation = runner.invoke(cli_main.app, ["schema", "--table", "no_such_table"])
    assert invocation.exit_code == 3


def test_bench_refuses_a_single_repeat_without_running_the_suite():
    """``--runs 1`` is a request for variance across one sample. Exiting 2 here
    is what stops an hour-long suite running to produce a report that cannot
    say anything -- and stops a scripted caller reading a 0 as a measurement."""
    result = runner.invoke(cli_main.app, ["bench", "--runs", "1"])
    assert result.exit_code == 3


def test_bench_rejects_an_unknown_baseline_before_spending_anything():
    """Same contract as ``sqe eval``: a typo in a rung name is caught at parse
    time, not three repeats into a run."""
    result = runner.invoke(cli_main.app, ["bench", "--baseline", "nonexistent"])
    assert result.exit_code == 3


def test_eval_rejects_an_unknown_baseline_without_running_anything():
    """Exit 3, not 1: a mistyped rung is a usage error, and running the suite
    against a silently-substituted default would record the wrong configuration
    under the right name -- which corrupts the accuracy timeline rather than
    just wasting a run."""
    invocation = runner.invoke(cli_main.app, ["eval", "--baseline", "no_such_rung"])
    assert invocation.exit_code == 3


def test_eval_rejects_an_unknown_suite():
    assert runner.invoke(cli_main.app, ["eval", "--suite", "nope"]).exit_code == 3


def test_eval_rejects_filters_that_match_no_cases():
    """An empty suite would otherwise report 0/0 as a clean pass."""
    invocation = runner.invoke(cli_main.app, ["eval", "--difficulty", "impossible"])
    assert invocation.exit_code == 3


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        (float("nan"), "-"),
        (2024.0, "2,024"),        # an integral float is a year or a count, not 2024.00
        (6_666_229.807, "6,666,229.81"),
        # Rates live below 1: at two decimals a ranked column of depletion rates
        # renders as a column of identical "0.14"s.
        (0.1436, "0.1436"),
        (True, "true"),
        (42, "42"),
    ],
)
def test_format_cell(value, expected):
    assert format_cell(value) == expected


def test_result_table_caption_reports_elided_rows():
    """Silently showing the first 30 of 200 rows would misrepresent the answer."""
    table = build_result_table([{"n": i} for i in range(200)], max_rows=30)
    assert table.caption is not None and "30 of 200" in table.caption


# ---------------------------------------------------------------------------
# Governance on the CLI surface
# ---------------------------------------------------------------------------


def test_as_flag_reaches_the_pipeline(install_pipeline):
    """Accepting the flag is not the feature; passing the identity on is."""
    fake = install_pipeline(_answer())
    assert runner.invoke(cli_main.app, ["ask", "revenue", "--as", "analyst_north"]).exit_code == 0
    assert fake.principals == ["analyst_north"]


def test_no_as_flag_runs_as_the_unrestricted_steward(install_pipeline):
    """The compatibility guarantee: an existing invocation is unchanged."""
    fake = install_pipeline(_answer())
    runner.invoke(cli_main.app, ["ask", "revenue"])
    assert fake.principals == ["steward"]


def test_an_unknown_principal_is_a_usage_error_not_a_query_failure(install_pipeline):
    """Nothing was asked of the warehouse, and a typo must not resolve to a default."""
    fake = install_pipeline(_answer())
    invocation = runner.invoke(cli_main.app, ["ask", "revenue", "--as", "nobody"])
    assert invocation.exit_code == 3
    assert fake.principals == []


def test_bench_from_results_reads_only_the_active_domain(tmp_path, monkeypatch):
    """The artefacts share one directory and one filename shape, so the newest N
    runs of a rung can straddle two warehouses. Taking the tail before filtering
    made `bench --from-results` fail on a mixed set -- the retail runs were there,
    they were just not the ones selected. Regression for 2026-09-21."""
    import json

    from semantic_query_engine.core.domains import active_domain

    results = tmp_path / "results"
    results.mkdir()

    def _write(name: str, domain: str) -> None:
        (results / name).write_text(
            json.dumps(
                {
                    "suite": "gold",
                    "baseline": "full",
                    "started_at": "2026-09-21T00:00:00+00:00",
                    "duration_seconds": 1.0,
                    "provider": "openai",
                    "generator_model": "m",
                    "domain": domain,
                    "ablation": {
                        "use_semantic_layer": True,
                        "use_validator": True,
                        "max_repair_attempts": 2,
                    },
                    "records": [],
                }
            ),
            encoding="utf-8",
        )

    active = active_domain().name
    _write("20260101T000000_gold_full.json", active)
    _write("20260102T000000_gold_full.json", active)
    # Newest, and from the other warehouse: the tail-first bug picked this one.
    _write("20260103T000000_gold_full.json", "other_domain")
    import evals.harness

    monkeypatch.setattr(evals.harness, "RESULTS_DIR", results)

    result = runner.invoke(cli_main.app, ["bench", "--from-results", "--runs", "2"])
    assert "more than one domain" not in result.output
    assert "Re-scoring 2 committed run(s)" in result.output
