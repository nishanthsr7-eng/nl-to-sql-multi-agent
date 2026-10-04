# Contributing to the Multi-Agent Natural Language to SQL System

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
python scripts/build_star_schema.py   # generates data/raw/star/*.csv -- gitignored, so a
                                      # fresh clone has to run this first
python scripts/init_database.py       # builds data/warehouse/semantic_query_engine.duckdb from them
sqe ask "Compare total revenue across all brands"   # verifies the console script is on PATH
```

Both domains' CSVs are generated under a fixed seed and are gitignored, so `init_database`
raises a `WarehouseError` naming the generator rather than half-building a warehouse. For
the airline domain:

```bash
python scripts/build_airline_domain.py
SQE_DOMAIN=airline python scripts/init_database.py
sqe --domain airline ask "What is the on-time rate for each carrier in 2024?"
```

A process serves one domain, selected before anything is constructed -- `--domain` on the
CLI, `SQE_DOMAIN` for the API and the scripts. `sqe domains` shows which is active.

Add `pip install -e ".[api]"` if you're working on the HTTP layer (`sqe serve`); the dev
extra already includes it so the API tests run.

## Before opening a PR

Run all three -- CI runs the same three on every push, so catching failures locally
is strictly faster:

```bash
ruff check .          # lint
mypy                  # type check
python -m pytest tests/unit -q   # fast, offline, no API key required
```

Use `python -m pytest`, not bare `pytest`, so `evals/` resolves on `sys.path`.

If your change touches an LLM-calling code path (`sql_generator.py`, `synthesis.py`,
`semantic/retriever.py`'s vector mode), also run the integration tier if you have a key
configured -- CI does **not** run this tier, so it's the one place a regression there can
hide:

```bash
python -m pytest -m integration
sqe eval --baseline ladder      # the gold suite itself; needs a configured provider
python -m evals.gate            # offline regression gate over committed runs
```

A suite run that fell back to the deterministic templates is not a measurement -- the
fallback engages silently on a provider error, so a rate-limited suite finishes in
under a second and reports the template registry's coverage as model accuracy. Runs
record `sql_source` and `EvalRun.degraded` gates both saving and `--fail-under`. Never
commit a degraded run to `evals/results/`.

## The two surfaces

`cli/` and `api/` are both thin wrappers over the same `AnalyticsPipeline`, and the rule
that keeps them honest is: **there is one payload**. Both render `QueryResult.to_dict()`;
neither reads the dataclass and neither builds a dict of its own. If you add a field to a
result, it appears in `--json`, in the HTTP body and (if you render it) in the terminal
view, automatically and in sync.

Two consequences worth knowing before you change either:

- `sqe ask --json` promises stdout carries nothing but the JSON document. Diagnostics go
  to stderr -- including all application logging, which `core/logging.py` already routes
  there. A `print()` added to a code path the CLI touches breaks `| jq`.
- The outcome mapping is a contract in two dialects: exit codes in
  `cli/exit_codes.py`, HTTP statuses in `api/app.py::_STATUS_BY_OUTCOME`. Change one and
  you almost certainly meant to change the other.

Re-record the README transcripts with `python scripts/record_demo.py` if you change the
renderer, the CLI's output, or the governance trace; it runs the real pipeline, so a stale
demo shows up as a diff rather than going unnoticed. `--scene demo|governance|evaluation`
records just one. The architecture diagrams are Mermaid sources rendered to SVG --
`docs/architecture/README.md` has the one command.

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
- **`evals/harness.py`** is the one gold-evaluation harness; `sqe eval` and
  `python -m evals.gate` are the two entry points onto it. Don't reimplement the loop
  a second time; add cases via `evals/datasets/build_gold_set.py` (see below). The
  unit tier tests the *scorer* -- accuracy arithmetic, the funnel, the safety axes,
  the gate -- because a suite that needs a model is not an offline check.
- **`tests/unit/test_validator_properties.py`** fuzzes the validator with `hypothesis`
  and asserts one invariant: no mutating statement ever validates. If you touch the
  validator's parse or safety path, run it. It found a live hole on its first run
  against an example-based suite that had been green for four phases.
- The audit log is off in the unit tier (`SQE_AUDIT=0`, set in
  `tests/unit/conftest.py` at *import* time, for the same reason the API keys are).

## Extension points

These are the places you'll actually add things; each one has a single home, by design
(see "The domain registry" in `ARCHITECTURE.md` for why this matters):

**Add or change a certified business metric** (e.g. a new formula, or fixing one) --
edit the target domain's `semantic_layer.json` (`data/semantic/` for retail,
`data/domains/airline/` for airline) and its `business_metrics` list only. The prompt,
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

**Add a gold-eval case** -- add it to `evals/datasets/build_gold_set.py` and re-run
that script. `gold_queries.json` is generated and must never be hand-edited: the build
executes every `reference_sql` first, so a case whose reference is broken cannot reach
a reported number. Give it the question, its `archetype`, `difficulty` and
`sql_features` labels (those are the stratification axes), the `required_columns` an
answer must carry, and a `reference_sql` that answers it -- ground truth is what that
query returns at eval time, so it survives a warehouse rebuild.

**Add a restricted-principal case** -- set `principal` to an id declared in
`data/domains/<domain>/principals.json`, and make `reference_sql` the *scoped*
answer. Two rules, both enforced by the build. The question must not name the thing
the policy filters on: if it does you are measuring the generator's ability to read a
`WHERE` clause out of English, which other cases already do, and the case would pass
against a pipeline with no row security at all. And the principal must be a
restricted one -- naming an unrestricted principal is rejected, because such a case
looks like a governance test, exercises no policy, and inflates the count of cases
the suite claims to govern.

**Add a table to the warehouse** -- emit its CSV from that domain's generator
(`scripts/build_star_schema.py` or `scripts/build_airline_domain.py`; never hand-write
one), point the domain manifest's `tables` map at it, and describe it in
`semantic_layer.json`'s `tables` list. There is no allow-list to update:
`Domain.allowed_tables` is *derived* from the semantic layer, so a table the layer doesn't
describe is not queryable and a second list can't disagree with the first. Then actually
wire it into at least one fallback template, few-shot example, or gold case -- an allow-listed table nothing ever
queries is dead validator surface area (this happened once; see `CHANGELOG.md`'s
`total_revenue` entry for what it costs).

If the table carries a scoping column or the anchor table's grain key, it must also
declare a row policy -- `GovernancePolicy.unguarded_tables()` is asserted empty for
both domains, so an unguarded table fails the suite rather than quietly serving every
row to every caller. That test is the point; don't add the table to an exclusion list.

**Add a whole domain** -- write `data/domains/<name>/domain.json` naming that domain's
semantic layer, warehouse, caches, gold set, principals, audit log and source CSVs, plus
a `build_command` quoted back to whoever hits a missing file. Add a generator script that
produces those CSVs under a fixed seed. No Python outside the manifest and the generator
should need to know the domain exists -- if you find yourself adding a branch on the
domain name, the thing you're branching on belongs in the manifest or the semantic layer.
New dirty data and new defects belong here rather than in `retail`, whose numbers are a
published baseline that every gold answer scores against.

**Add a principal, or change who holds what** -- edit
`data/domains/<domain>/principals.json`. Not the semantic layer: the layer describes
the warehouse and is versioned with it, while grants are a property of a deployment.
`unrestricted: true` must be spelled out; a principal with no `grants` key is a
half-written principal, not an unrestricted one. An empty grant list (`[]`) is a real
answer meaning "granted nothing", and compiles to `FALSE`.

**Tag a column as personal data** -- add it to the table's PII tags in that domain's
`semantic_layer.json`, with the masking strategy (pseudonymise, redact, partial).
Masking resolves against the parsed sources, so the tag follows the column through an
alias, a `SELECT *` and a CTE without further work.

## What not to do

- Don't add a second copy of a business formula or dimension value list outside
  `domain/registry.py`'s consumers -- that's the exact drift this registry exists to
  prevent (`ARCHITECTURE.md`).
- Don't read `os.environ` directly outside `core/config.py`.
- Don't hand-edit a generated file. `data/raw/star/`, `data/raw/airline/` and
  `evals/datasets/*gold_queries.json` all come from a script under a fixed seed; edit the
  script and re-run it. The exception is `evals/datasets/judge_labels.json`, which is
  hand-written by design and must never be generated.
- Don't catch a bare `Exception` and swallow it silently -- log it
  (`core.logging.get_logger`) and either degrade to a documented fallback (the pattern
  every LLM-calling agent already follows) or re-raise one of `core/errors.py`'s typed
  exceptions.
- Don't add a fallback template's literal via f-string interpolation when a bound `?`
  parameter would do the same job.
- Don't make an empty grant set compile to no predicate, or resolve an unknown
  principal id to a default. Both are the natural fix for an error you'll hit, and
  both hand the least privileged caller the most access.
- Don't make `scope_breaches()` or `report.executed_unsafely()` read the verdict they
  are checking. Both deliberately re-derive from the SQL that actually ran; a check
  that trusts the component it audits cannot catch that component silently stopping.
