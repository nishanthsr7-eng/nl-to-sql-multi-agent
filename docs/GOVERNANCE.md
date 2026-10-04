# Governance: who may see which rows, and what ran

Row-level security, PII masking, the audit log, and the fuzzing that found a hole.

The pipeline enforces access control at the query-plan layer -- the predicate is
injected into the parsed tree before execution, so it applies to SQL the model wrote
without the model being asked to cooperate. Three mechanisms, all verified by
execution rather than asserted.

![The same question answered unrestricted and as analyst_north, with the injected predicate visible in the SQL](demo/governance.svg)

*`python scripts/record_demo.py --scene governance`. The same question, the same
pipeline, two principals. The second one's SQL contains a semi-join nobody asked for,
the trace names the policy that put it there, and `sqe audit --verify` walks the hash
chain at the end.*

## Row-level security

A caller is a **principal**: an id, a role, a set of grants, and a PII clearance.
Principals live in `data/domains/<domain>/principals.json`, deliberately *not* in the
semantic layer -- the layer describes what the warehouse means and is versioned with
it, whereas who holds which grant is a property of a deployment and is the part a real
system reads from an identity provider. The domain declares the policy; the deployment
declares the identities.

Asking retail for total revenue as five different principals, the same question and
the same metric formula:

| `--as` | grants | total revenue |
| --- | --- | ---: |
| `steward` | unrestricted | 19,951,300.58 |
| `analyst_national` | PL-North, PL-South, PL-Central | 19,951,300.58 |
| `analyst_north` | PL-North | 6,664,220.52 |
| `analyst_south` | PL-South | 6,666,229.81 |
| `contractor` | *(granted nothing)* | no rows |

Two rows in that table are the ones worth reading twice.

**`analyst_national` reproduces the baseline to the cent while still being
filtered.** Its predicate is injected exactly as `analyst_north`'s is; it simply
excludes nothing. That is why it is kept as a principal distinct from the steward:
"sees everything" and "is not subject to the policy" are different states, and only
the second can hide a policy that silently stopped being applied.

**`contractor` reads zero rows, not every row.** An empty grant set compiles to
`FALSE`, never to an absent predicate. The reflex fix for the empty-`IN`-list syntax
error turns the least privileged principal into the most privileged, and it is the
single most common way row-level security is wrong in practice.

The predicate is a **semi-join on the fact's own key**, not a filter on a joined
dimension:

```sql
SELECT ROUND(SUM(units_sold * price_unit), 2) AS total
FROM fmcg_sales
WHERE fmcg_sales.store_id IN (
    SELECT dim_store.store_id FROM dim_store WHERE dim_store.region IN ('PL-North')
)
```

Scoping through a join to `dim_store` would have been shorter and would have leaked
every query that does not happen to join `dim_store`. Every `SELECT` in the tree is
scoped, not just the outermost one, or a CTE body reads the whole table.

**When the scoping dimension changes over time, the semi-join is closed on its
validity window too.** Airline's `fact_fuel` carries no carrier code and is scoped
through `dim_aircraft`, which is slowly-changing -- and two airframes transfer
operator mid-period. The untimed semi-join asks whether a tail number *ever* belonged
to a granted carrier, which handed both the old and the new operator the whole of that
airframe's fuel history: 323 rows and 6.2% too much fuel for `ops_northvale` alone.
The fix is a correlated `EXISTS` over a half-open window, so a changeover date belongs
to exactly one operator:

```sql
SELECT SUM(fuel_litres) FROM fact_fuel
WHERE EXISTS (
    SELECT 1 FROM dim_aircraft
    WHERE dim_aircraft.tail_number = fact_fuel.tail_number
      AND dim_aircraft.carrier_code IN ('NV')
      AND fact_fuel.fuel_date >= dim_aircraft.valid_from
      AND fact_fuel.fuel_date <  dim_aircraft.valid_to
)
```

No test caught the original, because every one of them asked whether a predicate was
*present* rather than what it admits. Two now assert the row counts, and both fail
against the old predicate.

Two further properties are structural rather than incidental:

- **`scope_breaches()` re-derives the verdict from the SQL that actually ran**, not
  from the injector's bookkeeping -- the same discipline as `report.executed_unsafely()`.
  It counts only top-level `AND` conjuncts, so a predicate smuggled under an `OR` does
  not read as scoped. A breach raises `IssueCode.ROW_POLICY_BREACH`.
- **Row policies are not ablatable.** `ValidatorAgent.safety_only()`, which the eval
  ladder uses to strip semantic checks, *refuses* a restricted principal rather than
  serving it unfiltered. The ladder ablates semantic checks; it never ablates access
  control.

`GovernancePolicy.unguarded_tables()` is the self-audit that stops this decaying: any
table carrying the scoping column, or the anchor table's grain key, must declare a
scope. A parametrised test asserts the set is empty for both domains, so a new table
added without a policy fails the suite rather than quietly serving everything.

## PII tagging and masking

Personal-data columns are tagged in the semantic layer and masked on the way out,
per the caller's clearance. The airline domain's `dim_crew` carries the tags; it was
added there rather than to retail because retail's figures are a protected baseline,
and it joins only to `dim_carrier` so that no airline eval number moved either.

Row scope and PII clearance are **independent axes**. `ops_northvale` and
`ops_northvale_hr` hold the same single-carrier grant and differ only in clearance:

```
ops_northvale     (pii=masked)
  {'crew_id': 'px_416123116a91', 'crew_name': '***', 'crew_email': '***@nv-crew.example'}
ops_northvale_hr  (pii=unmasked)
  {'crew_id': 'NV-CR-001', 'crew_name': 'Greta Adamczyk', 'crew_email': 'nv-cr-001@nv-crew.example'}
```

Three strategies are visible there -- pseudonymise, redact, and partial -- and masking
follows the column through an alias, a `SELECT *` and a CTE, because it resolves
against the parsed sources rather than matching output names.

A **derived expression over a tagged column is rejected, not masked**
(`IssueCode.PII_DERIVED`). `UPPER(crew_email)` discloses inside the warehouse before
a result row exists, so there is nothing left to mask by the time the value arrives;
masking it would be theatre. `COUNT` over a tagged column is exempt, since a count
discloses nothing about any individual:

```
derived over PII: ['UPPER(crew_email) computes over the personal-data column crew_email']
COUNT exempt:     []
```

## Audit log

Every run appends one record -- run id, timestamp, domain, principal, role, question,
SQL, outcome, row count, elapsed time, `sql_source`, issue codes, the policies applied
and the columns masked -- to an append-only JSONL log, for *every* outcome, including
clarifications and failures. A query that was refused is exactly the one an auditor
wants to find.

Records are **hash-chained**: each one's digest covers its own content and its
predecessor's hash, so editing a record in place breaks the chain rather than
rewriting history. `sqe audit --verify` re-derives every link and exits 1 on a
mismatch:

```
verify clean:     []
verify tampered:  ['record 1 (r1): content does not match its own hash -- this record was edited']
```

The log is off in the unit tier (`SQE_AUDIT=0`, set in `tests/unit/conftest.py` at
import time) and `data/audit/` is gitignored -- it is a deployment artefact, not
source.

## Fuzzing the safety invariant, and the hole it found

`tests/unit/test_validator_properties.py` uses `hypothesis` to generate SQL and
asserts the invariant that matters: **no mutating statement ever validates.** On its
first run it found a live defect --

```sql
TRUNCATE TABLE fmcg_sales; SELECT 1   -- validated as safe
```

Two independent causes. `sqlglot.parse_one` folds `a; b` into a single `exp.Block`,
which the "is this a SELECT?" check satisfied via `tree.find(exp.Select)`; and DuckDB
does execute both halves, confirmed by experiment. Separately, `TRUNCATE` was simply
absent from the mutation-node list, which named INSERT/UPDATE/DELETE/DROP/CREATE/ALTER
-- a list of the verbs somebody remembered.

The fix parses with plural `sqlglot.parse()` and refuses anything that is not exactly
one statement (`IssueCode.MULTIPLE_STATEMENTS`); one question produces one query, so a
semicolon in model output is never something to accommodate. The node list gained
`exp.Command`, which is what sqlglot parses anything it does not model into (INSTALL,
LOAD, CALL, VACUUM) -- SQL the validator cannot analyse is exactly what it must not
wave through. Regression tests sit in `tests/unit/test_validator.py` beside the
property tests that found it.

This is the honest argument for property testing in this repo: the example-based
tests were passing, and had been for four phases.

## Telemetry

A `run_id` contextvar tags every log line, human-readable or `SQE_LOG_FORMAT=json`,
so one run's lines can be pulled out of an interleaved log. `RunTrace` carries a
per-stage `SpanRecorder`; `stage_latency_ms` lands on every result payload and renders
under `--explain`, which turns "the pipeline felt slow" into a per-agent number. An
OpenTelemetry bridge is optional and is **not** a dependency -- it is a no-op unless
the SDK is installed, and `SQE_OTEL=0` disables it outright.

## Surfaces

```bash
sqe principals                          # identities this domain declares, and what each may see
sqe ask "total revenue" --as analyst_north
sqe repl --as analyst_north
sqe audit --verify                      # exits 1 on a broken chain
```

The API takes `principal` on `POST /query` and **403s when it is undeclared** rather
than defaulting -- an unknown principal id is an error, not an anonymous fallback,
because a typo in a caller's identity must not quietly become someone else's access.
`GET /principals` lists them.
