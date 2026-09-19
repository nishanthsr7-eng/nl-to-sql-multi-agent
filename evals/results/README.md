# Recorded eval runs

One JSON artefact per `EvalRun` -- one suite, one ablation rung, one point on the
accuracy timeline. Written by `sqe eval` and `sqe bench` via
`evals.harness.save_run`, read by `evals/gate.py` and `sqe bench --from-results`.

**These files are the measurement.** A number in `README.md` that no artefact here
supports is an assertion, not a measurement, and the regression gate compares the
newest run of a rung against the previous one *from this directory*. So:

- **Commit them.** An uncommitted artefact is one `git clean` away from being a
  re-run of the whole ladder. They are small, they are append-only, and the point
  of keeping them in git is that the accuracy timeline lives in history rather
  than in someone's terminal scrollback.
- **Never commit a degraded run.** `EvalRun.degraded` is recorded on the artefact
  itself; a run the deterministic fallback answered reports the template
  registry's coverage as model accuracy, and committing one enters a provider
  outage into the timeline as a genuine regression. `sqe eval` refuses to save
  one, and `evals/gate.py` excludes one from both sides of the comparison.
- **Don't commit a `--limit`ed smoke run.** It is a different denominator wearing
  the same filename shape, and `latest_run()` cannot tell the difference.
