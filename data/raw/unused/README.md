# Unused raw data

`fmcg_weekly_mi006_enriched.csv` and the four `batch_MI-006_2025-*.parquet` files are
not loaded by `init_database` (`src/semantic_query_engine/warehouse/duckdb_client.py`), not described
in `data/semantic/semantic_layer.json`, and not queried by any fallback template, few-shot
example, or gold-eval case.

They look like an earlier iteration of the weekly-modeling dataset, scoped to a single
SKU (`MI-006`), superseded by `data/raw/fmcg_weekly_modeling.csv` (loaded as
`weekly_modeling_data`, covering the full catalog). Kept here for reference rather than
deleted, in case the batch/enrichment pipeline they came from is revisited; if they are
still unused next time this directory is touched, delete them instead of moving them again.
