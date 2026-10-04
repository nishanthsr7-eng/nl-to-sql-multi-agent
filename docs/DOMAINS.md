# Two domains, one contract

Why a second warehouse exists, what it found, and its own ladder.

The architectural claim a semantic layer makes is that it is an *abstraction*, not a
config file for one CSV. That claim is cheap to assert and was, until Phase 4, not
checked anywhere. So the repo now carries a second warehouse in an unrelated vertical
and runs it through the identical pipeline:

```bash
sqe domains                                              # what this checkout can serve
sqe --domain airline ask "What is the on-time rate for each carrier in 2024?"
sqe --domain airline eval --baseline full                # the same harness, same report
```

| | retail | airline |
|---|---|---|
| Grain | one row per date, SKU and store | one row per flight date and flight number |
| Headline metrics | sums of money and units | *rates* over a count of flights |
| Second fact | `fact_inventory`, same grain | `fact_fuel`, **a different grain** |
| Gold set | 137 cases | 35 cases |

They share no table name, no metric name and no vocabulary. Adding the airline domain
touched no agent, no prompt and no validator rule: a domain is a directory under
`data/domains/` holding a `domain.json` (paths only) and a `semantic_layer.json`
(everything about the business). Getting there did require *removing* things from
Python -- the planner's keyword tuples, the FMCG clarification menu, the few-shot
bank and the system prompt's "FMCG analytics platform" were all literals about one
warehouse living in code that claimed to be domain-agnostic. They are now the
`language`, `few_shot_examples` and `example_questions` blocks of each domain's
semantic layer, and the retail values are byte-identical to the tuples they replaced,
so every number in [EVALUATION.md](EVALUATION.md) still describes the same planner.

**The second domain immediately found a bug in the first one's guardrail**, which is
the return on doing this at all. The fan-out check read "safe when the join keys cover
the full grain of at least one side". That is backwards for the side that *is*
covered: pinning `fact_fuel` to one row per (tail, day) is exactly what lets each fuel
row match all of that day's flights. In a star it never showed, because the covered
side is always a dimension and dimensions declare no additive measures. It showed the
first time a second fact joined a first, inflating `SUM(fuel_litres)` by 64% while the
validator said nothing. Which side is multiplied is now decided by the *other* side's
grain (`tests/unit/test_domains.py`).

## The airline ladder

The same four-rung ladder, run end to end against the airline warehouse on
2026-09-20 -- 0% fallback in every rung, no degraded run:

| Rung | Exec. acc. | Value acc. | Confidently wrong | p50 |
|---|---|---|---|---|
| naive | 65.4% | 57.7% | 7.7% | 7.5 s |
| + semantic layer | 65.4% | 57.7% | 7.7% | 11.2 s |
| + validator | 61.5% | 53.8% | 7.7% | 10.1 s |
| full (+ repair) | 61.5% | 53.8% | 7.7% | 10.4 s |

**Read this as 26 scored cases, because that is what it is.** One case moves the
number 3.8pp, so the step at the validator rung is three cases and nothing here
distinguishes a real effect from sampling noise. Containment is **100% across all
four rungs with zero breaches** -- the one figure not limited by the sample, since it
is re-derived from the SQL that actually ran rather than from the validator's verdict.

What the table is evidence for is that the *harness* travels: accuracy, strata, the
validator funnel and the safety axes all run unmodified against a warehouse sharing
no table, metric or vocabulary with retail. What it is **not** is a comparison with
the [retail ladder](EVALUATION.md#the-baseline-ladder). The gold sets differ in size, composition and metric shape --
rates over a count of flights versus sums of money -- so the two are not commensurable
and placing them side by side would invite a conclusion neither supports.

**The airline data is synthetic and generated under a fixed seed**
(`scripts/build_airline_domain.py`). The public on-time datasets are hundreds of
megabytes and do not belong in a portfolio repo; reproducibility is the property the
eval timeline actually needs. Nothing in this section is a measurement of real airline
operations.

## Dirty data, and the gold cases that assert correct handling

The airline warehouse carries three deliberate defects, all declared in its semantic
layer, because a warehouse whose defects are undocumented is a different and much
easier test than one where the model is told and still gets it wrong. They live here
rather than in the retail star because retail's totals are a published baseline --
revenue is 19,951,300.58 to the cent and every Phase 3 gold answer scores against it.

| Defect | Why it is nasty | Caught by |
|---|---|---|
| `arrival_delay_minutes` is NULL on ~1.5% of operated flights | The obvious formula `SUM(CASE WHEN delay <= 15 THEN 1 ELSE 0 END) / COUNT(*)` silently scores every *unknown* as **late**: the NULL falls to the ELSE branch and still counts in the denominator. The error is small and flattering-looking. | the certified metric counts the column, not the rows; `air_dirty_on_time_excludes_unreported` |
| Two tails re-registered mid-period, so `dim_aircraft` has two rows for each | A duplicate key in a dimension. Joining on `tail_number` alone inflates passengers by 5.7%. | the grain check -- `dim_aircraft` declares its grain as (tail_number, valid_from), so it is a slowly-changing dimension, not a lookup |
| Seven flights carry a 2019 date the feed should never have produced | They have no `dim_date` row, so a query joined to the calendar drops them and an unbounded aggregate does not: two "totals" that disagree by a number nobody reported. | `air_dirty_flights_outside_declared_period` |

The duplicate-key case forced a second fix, and it is the more interesting one. A
validity window is closed by *inequalities* (`f.flight_date >= ac.valid_from AND
f.flight_date < ac.valid_to`), which carry no equality key -- so the fan-out check
rejected the one join that returns the correct total. Rejecting both the wrong query
and the right one is not a guardrail, it is an outage. A table may now declare its
validity window in the semantic layer, and the check treats the grain as closed only
when **both** ends are declared *and* constrained. One end alone still fans out.
