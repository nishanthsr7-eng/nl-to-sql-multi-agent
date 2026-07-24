# Semantic Query Engine — Architecture Review & Hardening Roadmap

> **Status: historical.** This was the original hardening audit (2026-07-19). Most of
> its findings have since been implemented -- domain registry, typed errors,
> Pydantic-validated LLM output, split unit/integration tests, CI, README/ARCHITECTURE.md,
> repo hygiene. It's kept as-is for the reasoning behind those changes, not as a live
> status report. **See [`CHANGELOG.md`](../CHANGELOG.md) for what's actually landed,
> including a second pass that found and fixed issues that survived this first one.**

**Scope:** full read-through of `src/semantic_query_engine/**`, `tests/`, `evals/`, `scripts/`, `data/semantic/semantic_layer.json`, `pyproject.toml`, and repo/process state (git, CI, docs) as of 2026-07-19.

**How to read this:** findings are ordered by architectural weight, not file order. Each one names the exact location, why it caps the system's ceiling, and the concrete fix. This is written as if Semantic Query Engine were going into a portfolio, a technical interview, or a real production handoff — the bar is "would a senior engineer sign off on this," not "does the demo run."

---

## 1. What's already right

Worth stating plainly before the critique, because the fixes below build on these, they don't replace them:

- **`ValidatorAgent` uses `sqlglot` to parse SQL into a real AST** (`agents/validator.py`) instead of regexing strings. Table/column allow-listing, DDL/DML rejection, LIMIT bounds, and a "metric contract" check (revenue must be computed via the certified formula, not just aliased) are all structurally enforced. This is a genuinely professional pattern — most NL→SQL demos skip this entirely.
- **The LLM-with-deterministic-fallback shape** in `SQLGeneratorAgent` and `SynthesisAgent` (try the LLM, catch, log, degrade to a rule-based path) is the right instinct for a system that has to survive a missing API key or a provider outage. It just isn't applied consistently (§4).
- **Config is centralized** (`core/config.py`) — one dataclass for LLM settings, one for pipeline limits, nothing reads `os.environ` ad hoc elsewhere. That's a real discipline most projects at this size skip.
- **The semantic layer concept** (`data/semantic/semantic_layer.json` describing tables and certified metric formulas, retrieved via `SemanticRetriever`) is the correct shape for grounding an LLM against a real schema — it's just not the *only* place those formulas live (§2).
- **A repair loop with bounded retries** (`orchestrator.py`, `MAX_REPAIR_ATTEMPTS`) feeding validator errors back into the generator is a legitimate self-correction pattern, not just "generate once and hope."

None of what follows is "start over." It's "the good instincts here aren't finished being applied."

---

## 2. Single source of truth is violated for every business rule

This is the highest-leverage fix in the whole review, because it's the same mistake repeated four times.

The **revenue formula** (`units_sold * price_unit`) and the **stock depletion formula** (`units_sold / stock_available`) each exist independently in:

1. `data/semantic/semantic_layer.json` — `business_metrics[].definition` (the *intended* single source of truth)
2. `src/semantic_query_engine/prompts/sql_generation.py` — hardcoded again in `SYSTEM_ROLE` as rules 3–4 ("Compute revenue using ONLY the certified formula: SUM(units_sold * price_unit)")
3. `src/semantic_query_engine/agents/sql_generator.py` — hardcoded a third time inside the ~340-line fallback template bank (`_sql_sku_lookup`, `_detect_metric_expr`, the promo/stock branches)
4. `src/semantic_query_engine/agents/validator.py` — re-encoded a fourth time as an AST pattern-match in `_validate_metric_contract`

Same problem, smaller scale, for **dimension value allow-lists** (region, channel, category, brand, pack type):

- `agents/planner.py` — `_KNOWN_CATEGORIES`, `_KNOWN_CHANNELS`, `_KNOWN_PACK_TYPES` class tuples
- `prompts/sql_generation.py` — a second hardcoded literal list in `SYSTEM_ROLE` ("Available dimension values: region: 'PL-North', ...")
- `agents/sql_generator.py` — a third implicit copy via keyword matching (`"pl-north"`, `"pl-south"`, region maps in `_sql_sku_lookup`)

**Why it matters:** the moment someone adds a brand, renames a channel, or changes how promotional uplift is computed, they must find and edit 3–4 files correctly or the system silently drifts — the validator could end up enforcing a formula the prompt no longer teaches, or the fallback could compute something the validator then rejects. This is exactly the kind of bug that's invisible in a demo and expensive in production.

**Fix:** make `semantic_layer.json` load-bearing, not decorative.

- Add a `MetricRegistry` (thin wrapper already half-built as `SemanticLayer`/`load_semantic_layer`) that is the *only* place a formula string exists. `prompts/sql_generation.py` should interpolate `metrics` into the prompt (it already receives them — `build_business_metric_block` — so stop *also* hardcoding them in `SYSTEM_ROLE`). `validator._validate_metric_contract` should check against the same registry's column requirements, derived from the formula's referenced columns, not a second hand-written rule per metric.
- Add `dimension_values` (or derive them once via `SELECT DISTINCT` at warehouse-init time and cache) to the semantic layer JSON, and have `planner.py`, `sql_generator.py`, and `prompts/sql_generation.py` all import from that one list.

---

## 3. The "Planner Agent" isn't an agent — and that inconsistency is a bigger problem than it looks

`PlannerAgent.run()` (`agents/planner.py`, 377 lines) is 100% regex/keyword heuristics — tuples of trigger words (`_COMPARATIVE_HINTS`, `_DIAGNOSTIC_HINTS`), a cascade of `if has_sku_anchor and has_time and not (...)` branches, and hand-written ambiguity detection. It never touches an LLM.

That's a defensible design on its own. It is *not* defensible as part of a system that calls itself "multi-agent" and gives every other reasoning step (SQL generation, repair, synthesis) an LLM path with graceful fallback. Right now the architecture has three agents that follow one pattern and one that quietly follows a different one, and nothing in the code marks that as a deliberate choice — it reads as an unfinished corner, not a decision.

**Fix — pick one and make it explicit:**
- **Option A (recommended for "deep worth"):** give `PlannerAgent` the same LLM-primary / deterministic-fallback shape as `SQLGeneratorAgent`. Intent classification is a cheap, high-value LLM call (small prompt, structured output), and it would materially improve archetype accuracy on questions the current keyword lists don't anticipate (anything that doesn't contain "compare," "vs," "which," etc. gets misrouted today by construction).
- **Option B:** keep it deterministic, but *own that* — rename the class/docs to describe it as a fast, zero-cost, explainable pre-classifier by design (a legitimate architecture: cheap rule-based routing before expensive LLM calls), and add a code comment/ADR explaining why it's the one agent that doesn't call an LLM. Either is fine. Silence is the problem.

---

## 4. The deterministic SQL fallback is 340 lines of procedural branching

`SQLGeneratorAgent._generate_fallback` (`agents/sql_generator.py:135-393`) is a single method with ten numbered sections, each a cascade of `if has_promo / has_stock / has_yoy / has_wow / has_trend / has_brand / has_channel / ...` flags computed from keyword membership, each branch hand-building an SQL string. It's genuinely hard to reason about which branch a given question falls into without tracing the whole function, and adding one new question shape means threading a new `elif` into an already-long chain without breaking the ones above it.

**Fix:** replace the procedural cascade with a declarative template registry:

```python
@dataclass
class QueryTemplate:
    name: str
    matches: Callable[[str], bool]      # e.g. lambda q: "year over year" in q
    render: Callable[[str], str]        # returns the SQL string

TEMPLATES: list[QueryTemplate] = [
    QueryTemplate("sku_lookup", matches=_is_sku_lookup, render=_render_sku_lookup),
    QueryTemplate("year_over_year", matches=_is_yoy, render=_render_yoy),
    ...  # priority order == list order
]

def _generate_fallback(self, question: str) -> SQLGenerationResult:
    for template in TEMPLATES:
        if template.matches(question.lower()):
            return SQLGenerationResult(sql=template.render(question), source="fallback")
    return SQLGenerationResult(sql=_render_generic(question), source="fallback")
```
Same behavior, but each template becomes an independently testable, independently reviewable unit instead of one paragraph inside a 340-line function. This also makes the single-source-of-truth fix in §2 mechanical: each `render` pulls its formula from the metric registry instead of typing it out again.

---

## 5. Dead abstractions: the code defines patterns it doesn't use

- **`core/errors.py`** defines a full typed exception hierarchy — `ClarificationNeededError`, `SQLValidationError`, `QueryExecutionError`, `NoDataError` — with a docstring explicitly stating the intent: *"Agents raise these instead of returning ad-hoc error dictionaries, so callers... can branch on exception type rather than on string content."* Nothing in `orchestrator.py` raises or catches any of them. Instead, `AnalyticsPipeline.run()` returns four different hand-built dicts (`{"needs_clarification": ...}`, `{"error": "SQL validation failed", ...}`, `{"error": "Query execution failed", ...}`, `{"error": "No data matched the request", ...}`) and the UI branches on `isinstance(response, dict)` plus string-matching-adjacent dict keys — precisely the pattern the exception module says it exists to avoid.
- **`pipeline/state.py`** defines `PipelineState`, a dataclass meant to carry state through the pipeline. It's constructed once in `orchestrator.run()` and then treated as a write-only scratchpad (`state.intent = ...`, `state.sql = ...`) — every agent still receives its inputs as explicit positional/keyword arguments, not via `state`, and `state` itself is never returned or read back by anything. It's not wrong to have a trace-accumulator object, but calling it `PipelineState` and having it *not* actually flow through the system as state is misleading — either use it as the real argument each agent receives and mutates, or rename it to what it actually is (`AgentTrace`/`RunLog`) and stop implying more structure than exists.

**Fix:** either wire these abstractions in for real (raise/catch the typed exceptions in the orchestrator; pass `PipelineState` into each agent method instead of loose args) or delete them. An unused "clean architecture" pattern sitting next to the ad hoc code it was meant to replace is worse than not having it — it signals the codebase doesn't fully trust its own abstractions.

---

## 6. LLM output is trusted without a schema

Every LLM call (`sql_generator.py._generate_with_llm`/`_repair_with_llm`, `synthesis.py._synthesise_with_llm`) does:

```python
payload = json.loads(response.choices[0].message.content or "{}")
sql = self._clean_sql(payload.get("sql", ""))
```

There's no schema validation of the JSON shape, no retry on a malformed/incomplete response, and `.get(key, default)` silently produces an empty string or `"bar"` chart recommendation rather than surfacing that the model returned something unexpected. `response_format={"type": "json_object"}` guarantees valid JSON, not the *right* JSON — a model that omits `"sql"` entirely degrades silently into an empty-string SQL statement that then fails validation for an opaque reason three layers away from the actual cause.

**Fix:** define `Pydantic` models (`SQLGenerationPayload`, `SynthesisPayload`) and parse with `model_validate_json`, catching `ValidationError` explicitly (it already flows into the existing `except Exception` fallback path — this just makes the failure mode legible in logs/trace instead of an unexplained empty result). Pair with one retry-with-backoff (e.g. `tenacity`) before falling back to the deterministic path, since a transient network blip currently gets treated identically to "no API key configured."

---

## 7. DuckDB connections are never closed, and the app opens a fresh one every rerun

`warehouse/duckdb_client.get_connection()` calls `duckdb.connect(...)` with no caching and no `close()` anywhere in the codebase. `AnalyticsPipeline.__init__` calls it once and holds it for the pipeline's lifetime (fine, since the pipeline itself is cached in `st.session_state`) — but `ui/app.py:main()` *also* calls `get_connection()` directly on every `main()` invocation, i.e. **on every single Streamlit rerun** (every button click, every chat message, every widget interaction), and the previous connection object is simply dropped, never closed.

**Why it matters:** this is a real resource leak in a long-running Streamlit process — file handles / internal DuckDB state accumulate for the life of the server process across every user interaction in every session. It's exactly the kind of thing that looks fine in a demo (one person, one short session) and degrades a long-lived deployment.

**Fix:** use Streamlit's actual resource-caching primitive — `@st.cache_resource` — for the connection, so it's created once per process and reused:

```python
@st.cache_resource
def get_shared_connection() -> duckdb.DuckDBPyConnection:
    return get_connection()
```
and drop the redundant direct call in `main()` in favor of the same cached accessor the pipeline uses.

---

## 8. SQL is built by string interpolation everywhere, not parameter binding

Every fallback template (`sql_generator.py`) and the SKU lookup builder embed values directly into SQL strings: `f"sku = '{sku}'"`, `f" WHERE CAST(date AS DATE) BETWEEN '{year}-01-01' AND '{year}-12-31'"`, region filters via string formatting. Today this is *safe by construction* only because the values are extracted through narrow regexes (`[A-Z]{2}-\d{3}` for SKU, a fixed `region_map` dict) before interpolation — there's no path for a raw user string to reach the SQL text unescaped. That's a fragile safety property: it holds only as long as every future template author remembers to whitelist-extract before interpolating, and DuckDB's Python API supports bound parameters natively (`conn.execute(sql, [params])`, already used correctly in `warehouse/duckdb_client.py:init_database`).

**Fix:** route every fallback template's literal values through `?` placeholders and a params list, the same way `init_database` already does for the CSV paths. This is defense-in-depth, not a fix for a currently-exploitable bug — but "currently safe because of a regex" is not the same claim as "safe by design," and a reviewer should be able to tell which one they're looking at.

---

## 9. The test suite is not actually a unit test suite

Several compounding issues here, all pointing the same direction:

- **`tests/test_config.py::test_environment_variables_loaded_from_dotenv`** asserts `os.getenv("OPENAI_API_KEY")` is truthy — the test suite *requires a real API key to be present in `.env`* to pass at all. On a clean checkout, or in CI without a secret configured, this test fails before anything interesting is tested.
- **No LLM call is ever mocked.** `test_orchestrator.py`, `test_gold_evaluation.py`, and (indirectly, if a key is set) `test_sql_generator.py` all construct a real `AnalyticsPipeline()` and call `.run(...)`, which — whenever a key is present — makes live network calls to Groq/OpenAI for SQL generation *and* synthesis, for every test, every run. That makes the suite slow, non-deterministic (LLM output varies run to run), costs real money per CI run, and means a flaky network connection fails your test suite, not just your app.
- **`tests/conftest.py`** rebuilds the warehouse with `init_database(force=True)` at session scope against the **same DuckDB file path the live app uses** (`DUCKDB_PATH`, no override for tests). Running `pytest` deletes and rebuilds the exact database file the Streamlit app is reading from. Tests and the running app share mutable state with no isolation.
- **The gold-eval harness is duplicated verbatim.** `evals/run_gold_eval.py` and `tests/test_gold_evaluation.py` implement the same loop over `gold_queries.json` with the same three assertions, admitted in the former's own docstring ("This is the same dataset tests/test_gold_evaluation.py asserts against"). Any change to what "pass" means has to be made twice, correctly, or the two silently diverge.
- **The gold set itself is 5 cases** (`evals/datasets/gold_queries.json`) checking only `intent` and `required_columns` present — not that the returned *values* are numerically correct. A system whose core value proposition is "governed SQL you can trust" has no eval that actually checks a query's output against a known-correct answer.

**Fix (this is the deepest lever for "worthy" in the whole review):**
1. Split `tests/` into `tests/unit/` and `tests/integration/`. Unit tests inject a fake LLM client (define a small `Protocol`/interface the agents depend on instead of calling `build_client` directly, so a `FakeChatClient` returning canned JSON can be substituted) and run with zero network access, zero API key requirement, in milliseconds.
2. Give tests their own ephemeral DuckDB (`monkeypatch.setenv("SQE_DUCKDB_PATH", str(tmp_path / "test.duckdb"))`, or `:memory:`) so `pytest` never touches the developer's real warehouse file.
3. Delete the duplication between `evals/run_gold_eval.py` and `tests/test_gold_evaluation.py` — one should call the other, not reimplement it.
4. Grow the gold set past 5 cases and add value-level assertions (exact or tolerance-based numeric checks against precomputed expected results), not just "the right columns exist." Mark it `@pytest.mark.integration`, require an API key only for that marker, and skip it cleanly in CI when no key secret is configured.

---

## 10. No conversation memory across a clarification turn

When `PlannerAgent` flags a question `AMBIGUOUS` (Archetype D), the UI shows a clarification prompt and waits for the *next* chat message — but that next message is run through the pipeline as a brand-new, fully independent question. Nothing carries the original question forward. If a user asks "How did the promotion do?", gets asked to clarify, and replies "revenue, last month" — the second message alone (with no product scope, no explicit metric keyword the planner recognizes in isolation) may well loop back into another clarification request instead of being merged with the first message into one complete question.

**Fix:** when a clarification is pending (`st.session_state` already tracks message history), concatenate or explicitly pass the prior question alongside the follow-up into `PlannerAgent`/`SQLGeneratorAgent`, rather than discarding it. Even a simple `f"{previous_question}. {followup}"` heuristic before the next planner call would close most of this gap; a proper fix threads a `context: list[str]` of recent turns through the pipeline.

---

## 11. Embedding cache has no version key

`SemanticRetriever._initialize_embeddings` caches embeddings to `data/semantic/embedding_cache.json` keyed only by `f"table_{name}"` / `f"metric_{name}"` — not by embedding model name. If `SQE_EMBEDDING_MODEL` is ever changed (or the provider switches from OpenAI to something else with a different embedding model), the cache silently serves stale vectors computed by a different model, and cosine similarity scores become meaningless without any error or warning.

**Fix:** include the model identifier in the cache key (`f"{model}:table_{name}"`) so a model change naturally invalidates the relevant entries instead of silently corrupting retrieval quality.

---

## 12. Orphaned data and an underused table

- `data/raw/fmcg_weekly_mi006_enriched.csv` (348K) and four `batch_MI-006_2025-*.parquet` files (12K each) are **not referenced anywhere in `src/`** — not loaded by `init_database`, not mentioned in the semantic layer, not queried by any template. They're dead weight in the repo with no comment explaining whether they're future work or leftovers.
- `weekly_modeling_data` **is** loaded into DuckDB, documented in `semantic_layer.json` ("Aggregated weekly data containing advanced metrics and target variables for predictive modeling"), and allow-listed in the validator — but no fallback template, no few-shot example, and no gold-eval case ever actually queries it. It's schema-complete and functionally dark: the LLM path could theoretically reach it via retrieval, but nothing exercises or proves that it does.

**Fix:** either wire `weekly_modeling_data` into at least one fallback template + few-shot example + gold case (proving the second table is real, not aspirational), or explicitly scope it out and remove/relocate the orphaned raw files with a note on intended future use.

---

## 13. Process and repo hygiene (the "college project" tell, concretely)

- **There is no git history.** `.git/` exists but is empty — no commits, no branches, nothing tracked. For a system meant to demonstrate engineering maturity, the absence of any commit history (no incremental design decisions, no reviewable diffs, no blame trail) is the single most visible signal to anyone evaluating this repo. This should be the first thing fixed, independent of any code change: `git init` was seemingly run but never followed by an actual commit.
- **No CI.** No `.github/workflows/` — `ruff` and `pytest` are configured in `pyproject.toml` but nothing runs them automatically on push/PR.
- **No root `README.md`.** A visitor has no single entry point explaining what Semantic Query Engine is, how to run it, or how the pipeline works, without reading source.
- **No `ARCHITECTURE.md`**, despite already having two genuinely useful diagrams sitting unused in `docs/architecture/` (`distributed_lakehouse_architecture.png`, `multi_agent_orchestration.png`) that nothing in the repo links to or explains.
- **Assignment-shaped deliverables live inside the engineering repo**: `docs/design-brief/ai_solution_design_brief.pdf` and `docs/mvp-report/mvp_and_production_report.pdf` + 9 screenshots read as coursework/bootcamp submission artifacts, not repo documentation. Mixing "grader-facing PDF" with "engineer-facing source" makes the repo read as a class deliverable rather than a maintained system, even though the code underneath is more capable than that framing suggests.
- **No static type checking.** `ruff` is configured (`select = ["E", "F", "I", "UP", "B"]`) but that's lint, not type-checking — `mypy`/`pyright` isn't run anywhere, despite the codebase already being fairly consistently type-hinted (dataclasses, `from __future__ import annotations` everywhere). This is close to free given the existing hint coverage.

**Fix, roughly in order:**
1. Commit the current state with a real message; start using git properly going forward (feature branches or at least meaningful atomic commits).
2. Add `.github/workflows/ci.yml` running `ruff check` + `pytest` (unit tier only, per §9) on every push.
3. Write a root `README.md`: what it is, the 5-agent pipeline in one paragraph, how to run it, how to run tests/evals.
4. Write `ARCHITECTURE.md` that actually embeds/explains the two existing diagrams and documents the "LLM-primary, deterministic-fallback" pattern as a deliberate architectural decision (which it is — that's worth stating explicitly rather than leaving it implicit).
5. Move `design-brief/` and `mvp-report/` under something like `docs/submission-archive/` (or out of the repo into a separate deliverables folder) so the top-level `docs/` reads as engineering documentation.
6. Add `mypy` (or `pyright`) to the `dev` optional-dependencies group and to CI; the codebase's existing type hints mean this will likely surface real bugs cheaply.

---

## 14. Target module shape (summary of the above as one picture)

```
src/semantic_query_engine/
├── core/            # config, errors (ACTUALLY raised/caught), logging, llm_client
├── domain/          # NEW — MetricRegistry + DimensionRegistry, the one place
│                     # business formulas and allowed dimension values live.
│                     # semantic layer JSON loads into this; prompts, validator,
│                     # planner, and sql_generator all import from here instead
│                     # of hardcoding copies.
├── agents/
│   ├── planner.py          # LLM-primary + deterministic fallback (§3), or
│   │                       # explicitly documented as intentionally rule-only
│   ├── schema_retriever.py
│   ├── sql_generator.py    # templates as a registry (§4), params not
│   │                       # interpolation (§8), Pydantic-validated LLM output (§6)
│   ├── validator.py        # unchanged shape, now checks against domain/ registry
│   └── synthesis.py        # Pydantic-validated LLM output, retry-then-fallback
├── pipeline/
│   ├── orchestrator.py     # raises/catches core.errors types (§5), not dicts
│   └── state.py            # either genuinely threaded through agents, or
│                           # renamed to reflect what it actually is (a trace log)
├── warehouse/               # connection lifecycle via st.cache_resource (§7)
├── semantic/
└── ui/
tests/
├── unit/            # NEW — fake LLM client, ephemeral DB, no network, fast
└── integration/     # NEW — the current network-dependent tests, marked/gated
evals/               # calls into tests/integration or a shared harness (§9),
                     # gold set grown well past 5 cases with value-level checks
```

---

## 15. Prioritized roadmap

**Do first (cheap, high signal, no behavior risk):**
- Commit real git history (§13.1)
- Fix the DuckDB connection leak with `st.cache_resource` (§7)
- Key the embedding cache by model (§11)
- Split tests into unit (mocked) vs integration (real API), stop requiring a real key for `pytest` to pass at all (§9)
- Isolate the test database from the app's real warehouse file (§9)
- Delete the `evals`/`tests` gold-eval duplication (§9)
- Remove or explain the orphaned raw data files (§12)

**Do next (moderate effort, real architectural payoff):**
- Collapse the 4x-duplicated business formulas and 3x-duplicated dimension lists into one `domain/` registry (§2) — this alone fixes half the "feels fragile" surface area
- Refactor the 340-line fallback cascade into a template registry (§4)
- Wire `core/errors.py` exceptions into the orchestrator for real, or delete them (§5)
- Add Pydantic-validated LLM output parsing + one retry before fallback (§6)
- Add README + ARCHITECTURE.md + CI workflow (§13)

**Do later (larger, but this is where "deep worth" actually shows):**
- Make the Planner LLM-primary with fallback, matching the rest of the system (§3)
- Add conversation-context threading for the clarification loop (§10)
- Grow the gold-eval set with value-level correctness checks, not just shape checks (§9)
- Wire `weekly_modeling_data` into a real query path or formally scope it out (§12)
- Move parameter interpolation to bound params across all fallback templates (§8)

---

## Appendix — size/complexity snapshot

| File | Lines | Note |
|---|---:|---|
| `agents/sql_generator.py` | 476 | Dominated by the 340-line fallback cascade (§4) |
| `agents/planner.py` | 377 | 100% heuristic, no LLM path (§3) |
| `ui/app.py` | 285 | (already cleaned up in the UI pass) |
| `agents/synthesis.py` | 274 | LLM+fallback split is clean; output isn't schema-validated (§6) |
| `semantic/retriever.py` | 214 | Vector/keyword dual-mode is solid; cache key issue (§11) |
| `core/config.py` | 172 | Best-organized file in the repo |
| `agents/validator.py` | 169 | Strongest engineering in the codebase (§1) |
| `prompts/few_shot_examples.py` | 160 | Never exercises `weekly_modeling_data` (§12) |
| `pipeline/orchestrator.py` | 146 | Dict-based error handling instead of `core/errors.py` (§5) |

Total application code: ~3,100 lines across 29 modules — small enough that every fix above is a matter of days, not months.
