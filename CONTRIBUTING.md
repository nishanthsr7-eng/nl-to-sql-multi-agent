# Contributing to Semantic Query Engine

Development workflow, checks, and the extension points you'll actually touch. For
*what the system is and why it's built this way*, see [`ARCHITECTURE.md`](ARCHITECTURE.md);
for a punch list of what's already been fixed and when, see [`CHANGELOG.md`](CHANGELOG.md).

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -e ".[dev]"
cp .env.example .env          # optional -- add OPENAI_API_KEY or a Groq key (gsk_...)
                               # to exercise the LLM-primary path; everything works
                               # without one, on the deterministic fallback
python scripts/init_database.py   # builds data/warehouse/semantic_query_engine.duckdb from data/raw/*.csv
```

## Before opening a PR

Run all three -- CI runs the same three on every push, so catching failures locally
is strictly faster:

```bash
ruff check .          # lint
mypy                  # type check
pytest tests/unit -v  # fast, offline, no API key required
```

If your change touches an LLM-calling code path (`sql_generator.py`, `synthesis.py`,
`semantic/retriever.py`'s vector mode), also run the integration tier if you have a key
configured -- CI does **not** run this tier, so it's the one place a regression there can
hide:

```bash
pytest -m integration
python evals/run_gold_eval.py   # readable pass/fail report against evals/datasets/gold_queries.json
```

## Commit and PR conventions

- One logical change per commit; a commit message explains *why*, not just *what* --
  the diff already shows what changed.
- No `--no-verify`, no force-push to `main`.
- New behavior needs a test in `tests/unit/` (mocked LLM, ephemeral DuckDB -- see below).
  A bug fix needs a regression test that fails without the fix and passes with it.
- If you're fixing something non-obvious, add a line to `CHANGELOG.md` describing what
  was broken and why the fix works -- future-you (or the next contributor) won't have
  the context that's currently in your head.

## How the test tiers work

- **`tests/unit/`** -- offline, deterministic, milliseconds. `tests/unit/conftest.py`
  strips any real API key so every test runs the fallback/keyword path by default,
  regardless of what's in your `.env`. A test that wants to exercise the LLM-primary
  path injects `tests/unit/fakes.py::FakeChatClient` (or `RaisingChatClient`, to test
  the fallback-on-failure path) and monkeypatches a placeholder key back in for that
  one test -- see `test_sql_generator.py`'s `test_llm_path_*` tests for the pattern.
- **`tests/integration/`** -- hits the real, configured LLM provider. Marked
  `@pytest.mark.integration` and excluded by default (`addopts` in `pyproject.toml`).
  Skips cleanly with no key configured; costs real API calls when one is.
- **`tests/conftest.py`** points `SQE_DUCKDB_PATH` at a private temp file *before*
  any `semantic_query_engine` module is imported, so running the suite never touches
  `data/warehouse/semantic_query_engine.duckdb` (the file the running app actually reads).
- **`evals/gold_eval.py`** is the one gold-evaluation harness -- both the pytest
  gold-eval tests and the standalone `evals/run_gold_eval.py` report call into it.
  Don't reimplement the loop a second time; add cases to
  `evals/datasets/gold_queries.json` instead.

## Extension points

These are the places you'll actually add things; each one has a single home, by design
(see "The domain registry" in `ARCHITECTURE.md` for why this matters):

**Add or change a certified business metric** (e.g. a new formula, or fixing one) --
edit `data/semantic/semantic_layer.json`'s `business_metrics` list only. The prompt,
the validator's metric-contract check, and every fallback template that references the
metric all read from `domain/registry.py::MetricRegistry`, which loads this file --
you don't need to (and shouldn't) touch any of them separately.

**Add or change an allowed dimension value** (a new brand, region, category, ...) --
edit `semantic_layer.json`'s `dimension_values` (and `dimension_aliases`, if the
question might phrase it differently, e.g. "ready meal" -> `ReadyMeal`). The planner's
entity extraction and the SQL fallback's dimension matching both read from
`DimensionRegistry` -- again, one edit, not several.

**Add a new fallback query shape** (a question pattern the deterministic path doesn't
handle yet) -- add one `QueryTemplate(name, matches, render)` to the `TEMPLATES` list in
`agents/sql_generator.py`. `matches` takes a `_FallbackContext` (the question, its
lowercased form, and the metric registry) and returns `bool`; `render` returns a
`SQLGenerationResult`. Priority is list order, so put a more specific `matches` before a
more general one that would otherwise shadow it. Write the SQL through bound `?`
parameters for any literal extracted from free text (see `_render_sku_lookup` for the
pattern) rather than interpolating it into the string -- see the "SQL is parameterized,
not interpolated" note in `ARCHITECTURE.md`.

**Add a gold-eval case** -- add an entry to `evals/datasets/gold_queries.json` with the
question, expected `intent`, `required_columns`, and (where you can compute it) an
`expected_values` block for numeric correctness within tolerance, not just shape.

**Add a table to the warehouse** -- load it in `warehouse/duckdb_client.init_database`,
describe it in `semantic_layer.json`'s `tables` list, and add it to
`PIPELINE.allowed_tables` in `core/config.py`. Then actually wire it into at least one
fallback template, few-shot example, or gold case -- an allow-listed table nothing ever
queries is dead validator surface area (this happened once; see `CHANGELOG.md`'s
`total_revenue` entry for what it costs).

## What not to do

- Don't add a second copy of a business formula or dimension value list outside
  `domain/registry.py`'s consumers -- that's the exact drift this registry exists to
  prevent (`ARCHITECTURE.md`).
- Don't read `os.environ` directly outside `core/config.py`.
- Don't catch a bare `Exception` and swallow it silently -- log it
  (`core.logging.get_logger`) and either degrade to a documented fallback (the pattern
  every LLM-calling agent already follows) or re-raise one of `core/errors.py`'s typed
  exceptions.
- Don't add a fallback template's literal via f-string interpolation when a bound `?`
  parameter would do the same job.
