# Two stages, for one reason: the warehouses are built at *image build* time.
#
# The alternative -- building DuckDB from the CSVs on first request -- makes the
# first user of every new container pay a multi-second import, and makes a
# missing or malformed CSV a runtime 500 instead of a failed build. Baking it in
# means the image either works or never ships, and container start is just
# opening a file.
#
# The builder stage also lets the ~20 MB of source CSVs stay out of the final
# image: only the compiled .duckdb files are copied forward.

# --- builder ---------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# Every table a domain declares comes from its own generated CSVs. The retail
# fact is the star schema under data/raw/star/, NOT the flat
# fmcg_daily_sales_2022_2024.csv -- that file is the generator's input and is no
# longer loaded into the warehouse.
COPY data/raw/star/ ./data/raw/star/
COPY data/raw/airline/ ./data/raw/airline/
COPY data/raw/fmcg_weekly_modeling.csv ./data/raw/
COPY data/domains/ ./data/domains/
COPY data/semantic/ ./data/semantic/
COPY scripts/init_database.py ./scripts/

# SQE_PROJECT_ROOT is what core.config resolves every path from; setting it
# explicitly means the build does not depend on where the package happened to be
# installed inside site-packages.
ENV SQE_PROJECT_ROOT=/build

# Both domains are built, because the image serves either one and a domain whose
# warehouse is missing fails at first request rather than at build. The domain is
# process-global state selected before any agent is constructed, so it is set per
# invocation here rather than once in the environment.
RUN SQE_DOMAIN=retail python scripts/init_database.py \
 && SQE_DOMAIN=airline python scripts/init_database.py

# --- runtime ---------------------------------------------------------------
FROM python:3.11-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SQE_PROJECT_ROOT=/app

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[api]"

# domain.json owns paths, semantic_layer.json owns the business facts; the
# airline layer lives beside its manifest, retail's under data/semantic/.
COPY data/domains/ ./data/domains/
COPY data/semantic/ ./data/semantic/
COPY --from=builder /build/data/warehouse/ ./data/warehouse/

# Non-root, and the warehouse is read-only to it: nothing in the serving path
# writes to DuckDB, and a model-authored query that somehow got a mutation past
# the validator should still find the filesystem unwilling.
RUN useradd --create-home --uid 10001 sqe && chown -R root:root /app && chmod -R a+rX /app
USER sqe

EXPOSE 8000

# The API's own readiness check: it runs a query, so a corrupt warehouse reports
# unhealthy rather than serving 500s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,json; b=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz')); sys.exit(0 if b['status']=='ok' else 1)"

# `sqe serve` rather than uvicorn directly, so the container runs the same entry
# point a developer does and there is only one place that knows the app path.
ENTRYPOINT ["sqe"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
