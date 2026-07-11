# Semantic Query Engine

Natural-language analytics assistant for FMCG sales data. Ask a plain-English business
question; get governed SQL and a structured, business-ready answer back.

Early days: this is the project skeleton -- packaging, the configuration surface, typed
errors, and logging. The warehouse, semantic layer, and agent pipeline land on top of it.

## Getting set up

```bash
pip install -e ".[dev]"
cp .env.example .env   # optional -- an LLM key is only needed for the LLM-backed paths
```

Every filesystem path the app touches is resolved in
`src/semantic_query_engine/core/config.py` and overridable through the environment
(`SQE_PROJECT_ROOT`, `SQE_DUCKDB_PATH`), so tests and CI never have to write into a
developer's real data directory.

## Checks

```bash
ruff check .   # lint
mypy           # type check
pytest         # tests
```
