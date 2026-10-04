# Semantic cache

The off-by-default semantic cache and the three rules that bind it.

`sqe ask --cache` reuses a previous question's SQL when a new question means the same
thing, and `sqe cache` reports what it holds. It is **off by default**, including in
the eval harness, because a cached run reports a previous question's SQL at a cost of
zero -- measuring the cache and printing it as the model. `sqe eval --cache` opts in
and the report says so above the numbers, next to the hit rate and the tokens saved.

A naive semantic cache is a confidently-wrong-answer generator, which is the exact
failure this project exists to prevent, so three rules bind it:

1. **A hit never bypasses the validator.** Cached SQL is validated, LIMIT-injected,
   EXPLAINed and executed on the identical path as fresh SQL. The schema may have
   changed since the SQL was stored and only the validator can notice.
2. **Literals are a key, not a similarity.** "Revenue in 2023" and "revenue in 2024"
   differ by one character and embed at ~0.96 -- above any threshold that still lets a
   genuine rephrasing through. Every dimension value, identifier, date, month and
   `top N` in the question must match **exactly** before similarity gets a vote, so a
   near-hit can only ever change the phrasing, never the filter. The literals are read
   from the question text rather than from the planner, whose timeframe label is
   `year_detected` for both of those questions -- the guard cannot rest on it.
3. **Only SQL that validated *and* returned rows is stored.** A failed generation is
   not a cheaper way to fail next time.

With an embeddings-capable provider it is a genuine semantic cache; without one it
falls back to a deterministic lexical vector over token bigrams, which catches
reorderings and filler words but not synonyms. Which backend served a hit is counted
separately, because "40% hit rate" means two different things in the two modes.

Measured, on ten retail gold cases run twice against the local generator:

| | cold cache | warm cache |
|---|---|---|
| Generations served by the cache | 0.0% | **100.0%** |
| Generator tokens (in / out) | 37,779 / 1,624 | 5,263 / 1,136 |
| Tokens saved | -- | **32,516 in / 506 out** |
| Execution accuracy | 40.0% | 40.0% |
| Value accuracy | 40.0% | 40.0% |

**The accuracy column is the point; the hit rate is not.** 100% is trivially true
because the second pass asked the same ten questions -- a hit rate is only meaningful
over a repeated real-world query log, and this repo does not have one, so no "cut cost
40%" claim is made here. What the two rows do establish is that reusing the SQL
changed neither accuracy figure by a single case, which is the property a cache has to
have before its hit rate is worth quoting at all. The residual 5,263 input tokens are
synthesis, which still runs: the cache saves the *generation*, not the answer.
