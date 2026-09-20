"""The second domain, and the claim it exists to make checkable.

Phase 4 task 3 asserts that the semantic layer is an abstraction: a second
vertical should reach the same five agents through the same contract with no
code change. A claim like that is worth nothing asserted in a README, so it is
asserted here -- against a warehouse whose tables, metrics, grain and vocabulary
share not one name with the FMCG one the pipeline was written for.
"""

from __future__ import annotations

import dataclasses

import pytest

from semantic_query_engine.agents.planner import PlannerAgent
from semantic_query_engine.agents.validator import ValidatorAgent
from semantic_query_engine.core.domains import (
    DomainError,
    active_domain,
    available_domain_names,
    get_domain,
    set_active_domain,
)
from semantic_query_engine.domain.registry import load_domain_registries
from semantic_query_engine.prompts.few_shot_examples import few_shot_examples
from semantic_query_engine.prompts.sql_generation import build_system_role
from semantic_query_engine.semantic.layer import load_semantic_layer
from semantic_query_engine.warehouse.duckdb_client import init_database, open_cursor


@pytest.fixture
def airline(tmp_path_factory, monkeypatch):
    """Activate the airline domain against a throwaway warehouse.

    The warehouse is redirected because ``SQE_DUCKDB_PATH`` -- how the suite
    keeps its hands off the developer's real database -- deliberately applies to
    the default domain only, so a second domain would otherwise build its file
    inside the repo while the tests run.
    """
    domain = dataclasses.replace(
        get_domain("airline"),
        warehouse_path=tmp_path_factory.mktemp("airline") / "airline.duckdb",
    )
    monkeypatch.setattr("semantic_query_engine.core.domains._active", domain)
    yield domain


@pytest.fixture
def airline_cursor(airline):
    return open_cursor(init_database(force=True, domain=airline))


def test_both_domains_are_discovered_and_retail_is_the_default():
    assert {"retail", "airline"} <= set(available_domain_names())
    assert active_domain().name == "retail"


def test_an_unknown_domain_names_the_ones_that_exist():
    """The error has to be actionable: a typo is the common case here."""
    with pytest.raises(DomainError) as exc:
        get_domain("aviation")
    assert "retail" in str(exc.value)


def test_the_test_warehouse_override_does_not_leak_across_domains():
    """``SQE_DUCKDB_PATH`` must not point two domains at one file.

    It predates domains and is set by ``tests/conftest`` for the whole session.
    Honoured globally, the second domain loaded would open the first one's
    database and serve its tables under this domain's semantic layer -- every
    query failing on an unknown table, for a reason nothing in the output names.
    """
    assert get_domain("retail").warehouse_path != get_domain("airline").warehouse_path


def test_the_airline_domain_declares_nothing_the_retail_one_does(airline):
    """Shared table or metric names would weaken every other test in this file."""
    retail = load_semantic_layer(get_domain("retail").semantic_layer_path)
    air = load_semantic_layer(airline.semantic_layer_path)

    retail_tables = {t["table_name"] for t in retail.tables}
    air_tables = {t["table_name"] for t in air.tables}
    assert not (retail_tables & air_tables)

    retail_metrics = {m["metric_name"] for m in retail.metrics}
    air_metrics = {m["metric_name"] for m in air.metrics}
    assert not (retail_metrics & air_metrics)


def test_every_airline_few_shot_example_executes(airline_cursor):
    """The same guarantee the retail bank has, in the domain that inherits none of it."""
    for example in few_shot_examples():
        try:
            rows = airline_cursor.execute(example["sql"]).fetchall()
        except Exception as exc:  # pragma: no cover -- the message is the point
            pytest.fail(f"{example['question']}\n{example['sql']}\n{type(exc).__name__}: {exc}")
        assert rows, f"returns no rows: {example['question']}"


def test_the_planner_classifies_in_the_airline_vocabulary(airline):
    """No FMCG keyword is involved in any of these, which is the point."""
    planner = PlannerAgent()

    comparative = planner.run("Compare load factor by service model")
    assert comparative.intent.value == "comparative_analysis"

    diagnostic = planner.run("Which origin region has the worst average arrival delay?")
    assert diagnostic.entities["metric"] == "punctuality"

    ambiguous = planner.run("Tell me about flights")
    assert ambiguous.needs_clarification
    # The clarification offers airline metrics, not "revenue, units sold, stock
    # depletion" -- the retail literal that used to be compiled into the planner.
    assert "on-time rate" in (ambiguous.clarification_prompt or "")


def test_the_system_prompt_names_the_domain_it_is_serving(airline):
    assert "airline operations" in build_system_role()
    retail_language = load_domain_registries(
        load_semantic_layer(get_domain("retail").semantic_layer_path)
    ).language
    assert "FMCG" in build_system_role(retail_language)


def test_an_airport_code_is_matched_by_enumeration_not_by_shape(airline):
    """Three upper-case letters is a word, not an identifier.

    ``IdentifierRegistry`` applies every pattern case-insensitively, because
    identifiers are conventionally upper-case and questions are not. A
    shape-based ``\\b[A-Z]{3}\\b`` therefore matched "the", and the planner read
    "What is the on-time rate..." as a question scoped to airport THE -- enough
    to route it down the point-lookup branch on an anchor that does not exist.
    """
    identifiers = load_domain_registries().identifiers
    assert identifiers.find("airport_code", "What is the on-time rate for each carrier?") is None
    assert identifiers.find("airport_code", "How did LHR do in 2024?") == "LHR"


def test_a_domain_with_no_templates_returns_no_sql_rather_than_another_domains(airline):
    """The deterministic fallback is FMCG SQL; it must not answer an airline question.

    Empty SQL fails visibly one stage later. FMCG SQL against this warehouse
    either errors on an unknown table or -- if a name ever collided -- answers
    confidently from the wrong data, which is the failure this project exists to
    make impossible.
    """
    from semantic_query_engine.agents.sql_generator import SQLGeneratorAgent

    result = SQLGeneratorAgent()._generate_fallback("What is the on-time rate by carrier?")
    assert result.sql == ""
    assert result.source == "no_fallback"


# ---------------------------------------------------------------------------
# The fan-out direction bug the second domain found
# ---------------------------------------------------------------------------


def test_fanout_is_judged_by_the_other_sides_grain(airline, airline_cursor):
    """Covering one side's grain protects the *other* side, not the join.

    The rule used to read "safe when the keys cover the full grain of at least
    one side" and stop there. It is backwards for the side that is covered:
    pinning ``fact_fuel`` to one row per (tail, day) is exactly what lets every
    fuel row match all of that day's flights. In a star it never showed, because
    the covered side is always a dimension and dimensions declare no additive
    measures -- it took a second fact joining a first to expose it.

    The inflation is asserted against the warehouse rather than assumed, so this
    test fails if the generated data ever stops carrying the trap.
    """
    fanned_out = (
        "SELECT SUM(u.fuel_litres) AS litres FROM fact_fuel u "
        "JOIN fact_flights f ON u.tail_number = f.tail_number AND u.fuel_date = f.flight_date"
    )
    truth = "SELECT SUM(fuel_litres) AS litres FROM fact_fuel"
    inflated = airline_cursor.execute(fanned_out).fetchone()[0]
    actual = airline_cursor.execute(truth).fetchone()[0]
    assert inflated > actual, "the fuel fan-out trap is no longer in the generated data"

    validator = ValidatorAgent()
    result = validator.run(fanned_out, airline_cursor)
    assert not result.is_valid
    assert [issue.code for issue in result.issues] == ["grain_fanout"]


def test_the_safe_direction_of_the_same_join_still_passes(airline, airline_cursor):
    """The guard must not simply reject every fact-to-fact join.

    Summing a flights measure over the same join is correct: the keys cover
    fact_fuel's grain, so no flight row is duplicated. A check that rejected
    this too would be trading one wrong answer for a refusal to answer at all.
    """
    result = ValidatorAgent().run(
        "SELECT SUM(f.passengers) AS passengers FROM fact_flights f "
        "JOIN fact_fuel u ON f.tail_number = u.tail_number AND f.flight_date = u.fuel_date",
        airline_cursor,
    )
    assert result.is_valid, [issue.message for issue in result.issues]


def test_dimension_joins_are_unaffected(airline, airline_cursor):
    """A many-to-one join onto a dimension is the ordinary case and stays silent."""
    result = ValidatorAgent().run(
        "SELECT c.carrier_name, SUM(f.passengers) AS passengers FROM fact_flights f "
        "JOIN dim_carrier c ON f.carrier_code = c.carrier_code GROUP BY c.carrier_name",
        airline_cursor,
    )
    assert result.is_valid, [issue.message for issue in result.issues]


def test_set_active_domain_is_restored_between_tests():
    """A leaked active domain would make every later test read another warehouse."""
    assert active_domain().name == "retail"
    set_active_domain("retail")


# ---------------------------------------------------------------------------
# Deliberately dirty data (Phase 4 task 4)
# ---------------------------------------------------------------------------


def test_unreported_delays_are_null_not_zero(airline_cursor):
    """A missing delay must be missing, not a plausible number.

    Filling it with 0 would make every unreported flight count as perfectly
    on time, which is the failure mode this defect exists to expose -- and it
    would be invisible, because 0 is a value a real flight can have.
    """
    unreported = airline_cursor.execute(
        "SELECT COUNT(*) FROM fact_flights WHERE cancelled_flag = 0 "
        "AND arrival_delay_minutes IS NULL"
    ).fetchone()[0]
    assert unreported > 0


def test_the_naive_on_time_denominator_scores_unknowns_as_late(airline_cursor):
    """Why the certified formula counts the column, not the rows.

    ``COUNT(*)`` keeps the unreported flights in the denominator while the CASE
    sends them to its ELSE branch, so they are counted as late. The gap is small
    and in the flattering-looking direction, which is exactly why it needs a
    test rather than a comment.
    """
    naive, correct = airline_cursor.execute(
        "SELECT SUM(CASE WHEN arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), "
        "       SUM(CASE WHEN arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0 "
        "       / COUNT(arrival_delay_minutes) "
        "FROM fact_flights WHERE cancelled_flag = 0"
    ).fetchone()
    assert naive < correct


def test_a_duplicate_dimension_key_is_caught_rather_than_silently_doubling(
    airline, airline_cursor
):
    """Two rows for a re-registered tail inflate any measure joined to it.

    The defect is a duplicate key in a dimension -- the classic one -- and the
    guardrail that catches it is the grain check, because ``dim_aircraft``
    declares its grain as (tail_number, valid_from) rather than pretending
    tail_number is unique.
    """
    duplicated = airline_cursor.execute(
        "SELECT COUNT(*) FROM (SELECT tail_number FROM dim_aircraft "
        "GROUP BY tail_number HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert duplicated > 0

    naive = (
        "SELECT ac.manufacturer, SUM(f.passengers) AS passengers FROM fact_flights AS f "
        "JOIN dim_aircraft AS ac ON f.tail_number = ac.tail_number GROUP BY ac.manufacturer"
    )
    inflated = airline_cursor.execute(f"SELECT SUM(passengers) FROM ({naive})").fetchone()[0]
    truth = airline_cursor.execute(
        "SELECT SUM(passengers) FROM fact_flights WHERE cancelled_flag = 0"
    ).fetchone()[0]
    assert inflated > truth

    result = ValidatorAgent().run(naive, airline_cursor)
    assert not result.is_valid
    assert [issue.code for issue in result.issues] == ["grain_fanout"]


def test_the_correct_slowly_changing_join_is_allowed(airline, airline_cursor):
    """The guard must not make the right answer unreachable.

    A validity window is closed by inequalities, which carry no equality key, so
    a fan-out check that counted only equalities rejected the one join that
    returns the correct total. Rejecting both the wrong query and the right one
    is not a guardrail, it is an outage.
    """
    correct = (
        "SELECT ac.manufacturer, SUM(f.passengers) AS passengers FROM fact_flights AS f "
        "JOIN dim_aircraft AS ac ON f.tail_number = ac.tail_number "
        "AND CAST(f.flight_date AS DATE) >= CAST(ac.valid_from AS DATE) "
        "AND CAST(f.flight_date AS DATE) < CAST(ac.valid_to AS DATE) "
        "WHERE f.cancelled_flag = 0 GROUP BY ac.manufacturer"
    )
    result = ValidatorAgent().run(correct, airline_cursor)
    assert result.is_valid, [issue.message for issue in result.issues]


def test_one_end_of_a_validity_window_is_not_enough(airline, airline_cursor):
    """Only the lower bound leaves every later row matching, so it still fans out."""
    half_open = (
        "SELECT ac.manufacturer, SUM(f.passengers) AS passengers FROM fact_flights AS f "
        "JOIN dim_aircraft AS ac ON f.tail_number = ac.tail_number "
        "AND CAST(f.flight_date AS DATE) >= CAST(ac.valid_from AS DATE) "
        "WHERE f.cancelled_flag = 0 GROUP BY ac.manufacturer"
    )
    result = ValidatorAgent().run(half_open, airline_cursor)
    assert not result.is_valid
    assert [issue.code for issue in result.issues] == ["grain_fanout"]


def test_out_of_range_dates_make_two_totals_disagree(airline_cursor):
    """The defect is only interesting because the two obvious queries differ.

    Rows the feed stamped with a 2019 date have no ``dim_date`` row, so a query
    joined to the calendar drops them and an unbounded one does not. Identical
    totals would mean the defect had stopped being detectable.
    """
    unbounded = airline_cursor.execute("SELECT COUNT(*) FROM fact_flights").fetchone()[0]
    joined = airline_cursor.execute(
        "SELECT COUNT(*) FROM fact_flights f JOIN dim_date d ON f.flight_date = d.flight_date"
    ).fetchone()[0]
    assert unbounded > joined
