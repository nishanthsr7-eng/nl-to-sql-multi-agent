# Multi-Agent Natural Language to SQL System

Ask a plain-English business question; get validated, governed DuckDB SQL and a
structured, business-ready answer back. Five agents -- Planner, Schema Retriever, SQL
Generator, Validator (with a bounded repair loop) and Synthesis -- run over a declared
semantic layer, and serve two unrelated warehouses (FMCG retail and airline on-time
performance) with no code change between them.

The point of the project is not that an LLM can write SQL. It is that **wrong SQL gets
caught before a user sees it, and that this is counted**: an AST validator, grain-aware
fan-out detection, row-level security, PII masking and a hash-chained audit log all act
on the parsed query before execution, and an evaluation harness measures what each of
them buys.

## Highlights

- **Measured, not claimed.** A 137-case gold set with reference SQL, a four-rung
  ablation ladder, a validator funnel by rejection reason, and a CI gate over committed
  run artefacts. The honest headline: the guardrails do **not** raise accuracy -- they
  cut the confidently-wrong rate (7.6% -> 5.1%) by turning wrong answers into visible
  refusals. [Evaluation](docs/EVALUATION.md)
- **100% containment** on the adversarial suite across every rung, re-derived from the
  SQL that actually ran rather than from the validator's own verdict.
- **Governance at the query-plan layer.** Row policies are injected into the parsed tree
  and fail closed; PII is masked by clearance; every run is hash-chained into an audit
  log. [Governance](docs/GOVERNANCE.md)
- **Property-tested.** `hypothesis` fuzzing of the validator found a live stacked-statement
  hole (`TRUNCATE ...; SELECT 1`) that the example tests had missed for four phases.
- **Model matrix.** Three local models compared on the same pipeline: `qwen2.5-coder:7b`
  59.8%, `deepseek-coder-v2:lite` 52.8%, `mistral` 49.6% execution accuracy.
- **Two domains, one contract.** Adding the airline warehouse touched no agent, prompt
  or validator rule -- and found a fan-out bug in the first domain's guardrail.
  [Domains](docs/DOMAINS.md)

**Stack:** Python 3.10+, DuckDB, sqlglot, Pydantic, Typer + Rich, FastAPI, OpenAI-compatible
LLM APIs (Ollama, OpenAI, Groq), pytest + hypothesis, ruff, mypy, Docker, GitHub Actions.

## Demo

![sqe answering a question, showing the validator verdict, and refusing an ambiguous one](docs/demo/demo.svg)

*A real transcript, not a mock-up: `scripts/record_demo.py` runs the questions through the
actual pipeline and renderer. It shows a ranked answer; the same under `--explain` with the
SQL, per-agent trace and validator verdict; a question too vague to plan, refused with the
parameters it is missing; and a query that runs but finds nothing.*

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env   # optional: add an LLM key; without one the engine uses deterministic fallbacks
sqe ask "Compare total revenue across all brands"
```

The DuckDB warehouse is built on first use; rebuild it with `python scripts/init_database.py`.

```bash
sqe ask "total revenue by region" --explain    # + SQL, agent trace, validator verdict, timing
sqe ask "total revenue by region" --json | jq  # the raw result payload, nothing else on stdout
sqe repl                                       # multi-turn, clarification-aware
sqe ask "total revenue" --as analyst_north     # answered under that principal's row scope
sqe --domain airline ask "What is the on-time rate for each carrier in 2024?"
sqe eval --baseline ladder                     # the measurement itself (needs a provider)
sqe audit --verify                             # replay the audit chain; exits 1 if tampered
```

Exit codes are part of the contract: **0** answered, **2** clarification needed,
**1** failed, **3** bad usage or an unopenable warehouse.

## How it works

A five-agent pipeline turns a question like *"Which brand had the highest promotional
uplift?"* into validated DuckDB SQL and a narrative answer:

1. **Planner** classifies intent (lookup / comparative / diagnostic / needs clarification)
   and extracts entities -- one slot per identifier and dimension the active domain
   declares, plus timeframe and metric family. Its keywords come from that domain's
   semantic layer, not from Python.
2. **Schema Retriever** grounds the question against the active domain's semantic layer
   (table/column descriptions and certified business-metric formulas), via vector search
   when an embeddings-capable provider is configured, or keyword overlap otherwise.
3. **SQL Generator** produces SQL -- an LLM call when a provider is configured, a
   declarative template bank otherwise (see `src/semantic_query_engine/agents/sql_generator.py`).
   The templates are FMCG SQL, so a domain that does not declare them gets no fallback
   at all rather than another domain's queries.
4. **Validator** parses the SQL into an AST (`sqlglot`) and enforces table/column
   allow-lists, blocks DDL/DML, bounds `LIMIT`, and checks that any certified metric named
   in the question was actually computed via its registered formula. A bounded repair loop
   feeds validator errors back into the generator before giving up.
5. **Synthesis** turns the result set into a 5-layer response: narrative summary, key
   metric, comparison context, chart recommendation, and the SQL itself for audit.

Every LLM-calling agent follows the same shape: try the LLM, and on any failure (no API
key, a provider outage, a malformed response) degrade to a deterministic path instead of
breaking. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design and the registry
that keeps business formulas and dimension values in one place, and
[`CHANGELOG.md`](CHANGELOG.md) for what's been fixed and when. Contributing?
See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the dev workflow and extension points.

## More ways to run it

Other commands: `sqe schema` / `sqe schema --table fmcg_sales` and `sqe metrics` browse the
semantic layer without touching the warehouse; `sqe examples` prints starter questions.
`sqe bench --runs 3` repeats one rung and reports the spread and the flip rate;
`sqe bench --from-results` re-scores the last N committed runs without re-running
them. `sqe domains` lists the warehouses this checkout can serve and `sqe cache`
inspects or clears the current one's semantic cache.

### As a service

```bash
pip install -e ".[api]"
sqe serve --port 8000
curl -s localhost:8000/healthz | jq
curl -s localhost:8000/query -H 'content-type: application/json' \
  -d '{"question": "total revenue by region"}' | jq
```

`POST /query`, `GET /healthz`, `GET /schema`. The response body is the same payload
`--json` prints, and the HTTP status carries the same three-way distinction the exit code
does: 200 for an answer *or* a clarification (underspecified is not malformed), 422 when
the guardrails rejected the generated SQL or nothing matched, 504 on a query timeout. An
`X-SQE-Result-Kind` header lets a client branch without parsing the body. `/healthz`
reports whether an LLM is actually configured, because a keyless deployment silently
answers from the deterministic fallback -- a different system to the one you evaluated.

### In a container

```bash
docker compose up api                      # warehouses are baked in at image build time
docker compose run --rm cli ask "Compare total revenue across all brands" --explain
docker compose run --rm cli --domain airline ask "on-time rate by carrier"
SQE_DOMAIN=airline docker compose up api   # a process serves one domain; pick it here
```

## Tests, linting, and evals

```bash
ruff check .                   # lint
mypy                           # type check
python -m pytest tests/unit -q # unit tier only -- no network, no API key required
python -m pytest -m integration        # + real-provider tests (needs a configured provider)
python evals/datasets/build_gold_set.py  # regenerate the gold set (executes every reference)
sqe eval --baseline ladder             # the measurement itself (needs a provider)
python -m evals.gate                   # the regression gate, offline, over committed runs
```

Use `python -m pytest`, not `pytest`, so `evals/` resolves on `sys.path`.

`tests/unit/` mocks the LLM client and runs against an ephemeral DuckDB file (never the
developer's real warehouse); `tests/integration/` exercises the real configured provider
and is excluded by default (`addopts = "-m 'not integration'"` in `pyproject.toml`). The
unit tier tests the *scorer* -- accuracy arithmetic, the funnel, the safety axes, the
gate -- while the end-to-end gold suite is integration-tier, because a suite that needs a
model is not an offline check.

`tests/unit/test_validator_properties.py` fuzzes the validator with `hypothesis` and
asserts one invariant: no mutating statement ever validates. It earned its place by
finding a live hole on its first run -- see
[Governance](docs/GOVERNANCE.md#fuzzing-the-safety-invariant-and-the-hole-it-found).
The audit log is disabled in this tier (`SQE_AUDIT=0`), set at conftest *import* time
for the same reason API keys are: it has to be false before anything is constructed.

Two workflows, and the split between them is forced by the generator being local:

* [`ci.yml`](.github/workflows/ci.yml) runs lint, types, the unit tier and a CLI smoke
  test on every push. No model, no network.
* [`eval-gate.yml`](.github/workflows/eval-gate.yml) runs `python -m evals.gate` over the
  artefacts committed in `evals/results/`, failing a pull request that drops value
  accuracy more than 2pp against the previous run of the same baseline, or that records a
  containment breach. It does **not** run the suite: a GitHub-hosted runner cannot reach
  `localhost:11434`. The suite itself runs nightly on the development machine via
  [`scripts/nightly_eval.ps1`](scripts/nightly_eval.ps1) (Task Scheduler), which commits
  to a local `eval/nightly` branch and deliberately does not push — an unattended job
  that publishes an accuracy number before anyone has looked at it is how a bad run
  becomes the baseline.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full dev workflow and how to extend the
system (add a metric, a dimension value, a fallback template, or a gold case).

## Project layout

```
src/semantic_query_engine/
├── core/         # config, typed errors, logging, LLM client, Pydantic response schemas,
│                 # the QueryResult union (results.py), domain resolution (domains.py),
│                 # token accounting (usage.py) and the off-by-default semantic cache
├── domain/       # registry.py -- metrics, dimensions, identifiers and grain, all parsed
│                 # out of the active domain's semantic_layer.json at load time
├── semantic/     # semantic layer loading + retrieval (vector or keyword)
├── agents/       # planner, schema retriever, SQL generator, validator, synthesis
├── prompts/      # prompt assembly + few-shot examples for the LLM-backed agents
├── pipeline/     # orchestrator (chains the agents, typed-exception error handling)
├── warehouse/    # DuckDB connection lifecycle, guarded execution, schema dump
├── governance/   # policy + principals + row_security + masking + audit + telemetry --
│                 # policies from the semantic layer, identities from the deployment
├── cli/          # `sqe` -- typer + rich; render.py formats the same payload --json emits
└── api/          # FastAPI service over the identical pipeline (optional extra)
tests/
├── unit/         # fast, offline, no API key
└── integration/  # real LLM provider, marked and excluded by default
evals/
├── harness.py    # runs a suite; records sql_source, degradation and the funnel
├── report.py     # accuracy, strata, funnel, safety, row-policy and masking renderers
├── gate.py       # the CI regression gate -- breaches exit 1 regardless of accuracy
├── judge.py      # narrative scoring, calibrated against hand labels
├── grounding.py  # every figure in a narrative traced back to a returned row
├── model_matrix.py, variance.py, compare.py, schema.py
├── datasets/     # the gold-set *builders*; the JSON they emit is generated, not edited.
│                 # judge_labels.json is the exception -- hand-written, by design
└── results/      # committed run artefacts -- what the README's numbers are quoted from
data/
├── domains/      # per-domain semantic layer + principals (retail, airline)
├── raw/          # the source CSVs and the generated star schema
└── warehouse/    # the DuckDB files and per-domain caches (all generated, gitignored)
docs/
├── architecture/ # Mermaid sources + the SVGs rendered from them (see its README)
├── demo/         # the recorded CLI transcripts embedded above
└── EVALUATION.md, GOVERNANCE.md, DOMAINS.md, SEMANTIC_CACHE.md  # the long-form write-ups
scripts/          # warehouse init, star-schema and airline generators, demo recorder,
                  # the nightly eval job
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the architecture diagrams and the reasoning
behind the LLM-primary / deterministic-fallback pattern used throughout.

## License

[MIT](LICENSE)
