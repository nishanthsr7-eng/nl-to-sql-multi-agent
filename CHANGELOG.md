# Changelog

Notable fixes and hardening passes, newest first. `docs/ARCHITECTURE_REVIEW.md` is
the original, now-historical hardening roadmap (2026-07-19) that most of the earlier
entries below trace back to; this file tracks what actually landed and when.

## 2026-07-24 -- Post-review hardening pass

A follow-up audit after the review below found several issues that survived the
first hardening pass. Fixed one by one, each with test coverage:

- **Repair loop was a no-op without an LLM.** `SQLGeneratorAgent.repair()`
  (`agents/sql_generator.py`) fed the same `question` back into
  `_generate_fallback`, which is a pure function of `question` alone -- so every
  repair attempt reproduced byte-for-byte the SQL that had just failed
  validation, silently burning both of `MAX_REPAIR_ATTEMPTS` for nothing. This
  broke the "bounded self-correction" behavior for the entire no-API-key path.
  Fixed by having `repair()` report `source="fallback_unrepairable"` when the
  fallback reproduces the failed SQL, and having the orchestrator
  (`pipeline/orchestrator.py`) stop retrying as soon as a repair attempt returns
  identical SQL to the one that just failed, instead of exhausting the budget.
- **Brand extraction had drifted from the domain registry.** `PlannerAgent._extract_brand`
  (`agents/planner.py`) used a hardcoded regex (`\b(mi|yo|re|sn|ju)brand\d\b`)
  instead of `DimensionRegistry.match("brand", ...)`, the one path every other
  dimension (region, category, channel, pack_type) already used. This
  contradicted the module's own docstring claim that entity extraction can't
  drift from the registry, and meant a new brand added to
  `semantic_layer.json` wouldn't be recognized by the planner. Now reads from
  the registry like every other dimension.
- **Validator's LIMIT parsing didn't catch every failure mode of the cast it
  guards.** `int(limit.expression.this)` in `ValidatorAgent.run`
  (`agents/validator.py`) was wrapped in `except ValueError`, but a
  non-standard literal node can raise `TypeError` instead. Broadened to
  `except (ValueError, TypeError)`.
- **`promotion_flag` type mismatch between tables was undocumented.**
  `fmcg_sales.promotion_flag` is `INTEGER`; `weekly_modeling_data.promotion_flag`
  is `VARCHAR` (`'True'/'False'`). The certified `promotional_uplift` formula
  (`promotion_flag = 1`) is written for `fmcg_sales` and would misbehave if
  ever pointed at the other table. Documented explicitly in
  `data/semantic/semantic_layer.json` so a future template/prompt author
  doesn't apply it to the wrong table.
- **DuckDB connection failures crashed the app with a raw traceback.**
  `warehouse/duckdb_client.py` had no error handling around
  `duckdb.connect()` or table creation -- a locked or corrupt warehouse file
  propagated an untyped `duckdb.Error` straight out of `AnalyticsPipeline.__init__`.
  Added `core.errors.WarehouseError` and wrapped both `init_database` and
  `get_connection` to raise it with an actionable message; `ui/app.py` now
  catches it at startup and shows `st.error` instead of crashing.
- **Malformed `.env` could crash the app at import time.** The manual `.env`
  parser used when `python-dotenv` isn't installed (`core/config.py`) had no
  error handling around `read_text()` -- a non-UTF-8 or BOM-prefixed file
  raised `UnicodeDecodeError` before logging was even configured. Extracted
  into `_load_env_file_manually` (independently testable) and made it a safe
  no-op on a read/decode failure instead of crashing.
- **Dead configuration: the `total_revenue` DuckDB view.** Allow-listed as a
  queryable table (`PIPELINE.allowed_tables`) but never referenced by any
  fallback template, few-shot example, retrieval document, or gold case --
  pure validator surface area with no wiring behind it. Removed the view from
  `warehouse/duckdb_client.init_database` and the table from the allow-list.
- **Module-level mutable global in the SQL fallback templates.**
  `sql_generator.py` used a lazy-singleton global (`_metric_registry_cache`)
  for `_revenue_expr`/`_stock_expr`/`_detect_metric_expr`, even though the
  registry was already available on every template's `_FallbackContext.metrics`
  -- an implicit dependency sitting next to an otherwise dependency-injected
  design. All three now take the registry as an explicit argument.
- **Connection leak: one `AnalyticsPipeline` (and one DuckDB connection) per
  Streamlit session.** `ui/app.py` already cached one shared connection via
  `st.cache_resource` for dataset stats, but `AnalyticsPipeline()`'s default
  constructor opened a second, separate connection per browser session with no
  cleanup path. `AnalyticsPipeline.__init__` now accepts an optional `conn`,
  and `ui/app.py` passes the same cached connection into it.
- **Misleading "LLM connected" status.** The sidebar showed "LLM connected"
  as soon as an API key string was present (`ui/app.py`), before any actual
  call had succeeded -- a typo'd or expired key looked identical to a working
  one until a real question ran. Reworded to "LLM configured", which is the
  claim actually being made.

See `git log` for the corresponding commits and `tests/unit/` for the new
regression tests (`test_sql_generator.py`, `test_orchestrator_fallback.py`,
`test_planner.py`, `test_config.py`, `test_duckdb_client.py`).

## 2026-07-20 -- Architecture hardening (from `docs/ARCHITECTURE_REVIEW.md`)

Domain registry as single source of truth for certified metric formulas and
dimension values; typed exceptions (`core/errors.py`) wired into the
orchestrator for real; Pydantic-validated LLM output with one retry before
fallback; test suite split into `tests/unit/` (offline, fake LLM client) and
`tests/integration/` (real provider, opt-in); CI added (`ruff` + `mypy` +
`pytest tests/unit`); README, ARCHITECTURE.md, and repo hygiene (submission
artifacts moved under `docs/submission-archive/`). Full detail in
`docs/ARCHITECTURE_REVIEW.md`.
