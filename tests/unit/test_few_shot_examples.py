"""The few-shot bank is part of the prompt, so it is part of the contract.

These examples are injected into the SQL-generation prompt verbatim, which makes
them the strongest signal the model gets -- stronger, empirically, than the
retrieved schema context. When Phase 4 moved ``region`` to ``dim_store`` and
``brand``/``category`` to ``dim_product``, the bank kept selecting them off
``fmcg_sales``. Nothing failed: the examples are strings, nobody executed them,
and the only symptom was an 80% ``unknown_column`` rejection rate on the eval
suite that looked like the model being weak at SQL.

So the bank is tested the way the gold set is: by running it. An example that no
longer parses, resolves or executes is a prompt that teaches the model to write
invalid SQL, and that is a defect regardless of which model is configured.
"""

from __future__ import annotations

import pytest

from semantic_query_engine.prompts.few_shot_examples import few_shot_examples
from semantic_query_engine.warehouse.duckdb_client import get_connection, open_cursor

# Read once at collection: the bank now lives in the active domain's
# semantic layer, so this parametrises over whichever domain is being tested.
EXAMPLES = few_shot_examples()
IDS = [example["question"][:60] for example in EXAMPLES]


@pytest.fixture(scope="module")
def cursor():
    return open_cursor(get_connection())


@pytest.mark.parametrize("example", EXAMPLES, ids=IDS)
def test_every_few_shot_example_executes_against_the_warehouse(example, cursor):
    """Each example must run. This is what catches a column that moved tables."""
    try:
        cursor.execute(example["sql"])
    except Exception as exc:  # pragma: no cover -- the assertion message is the point
        pytest.fail(
            f"Few-shot example does not execute:\n{example['question']}\n\n"
            f"{example['sql']}\n\n{type(exc).__name__}: {exc}"
        )


@pytest.mark.parametrize("example", EXAMPLES, ids=IDS)
def test_every_few_shot_example_returns_rows(example, cursor):
    """An example that parses but matches nothing teaches a filter that is wrong.

    Separate from execution because the failures are different: a syntax or
    schema error is a broken example, an empty result is a stale literal -- a
    SKU, region or date range the regenerated warehouse no longer contains.
    """
    cursor.execute(example["sql"])
    assert cursor.fetchall(), f"Few-shot example returns no rows: {example['question']}"


def test_examples_touching_a_dimension_column_join_that_dimension():
    """The regression itself, asserted directly rather than via execution.

    Execution already catches this today, but only because DuckDB happens to
    reject the unknown column. Naming the rule keeps the guarantee if a future
    schema ever puts a same-named column back on the fact table, which would
    make the broken example silently executable and quietly wrong again.
    """
    owners = {
        "dim_store": ("region", "channel", "city", "population_tier", "store_format"),
        "dim_product": ("brand", "category", "segment", "unit_cost"),
    }

    for example in EXAMPLES:
        sql = example["sql"]
        if "fmcg_sales" not in sql:
            continue  # weekly_modeling_data is denormalised by design
        for table, columns in owners.items():
            if any(column in sql for column in columns):
                assert table in sql, (
                    f"{example['question']!r} references a {table} column "
                    f"but never joins {table}"
                )
