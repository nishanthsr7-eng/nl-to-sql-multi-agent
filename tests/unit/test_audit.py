"""The append-only audit log, and the two properties that make it worth having:
nothing that ran is missing from it, and an edit to it is detectable.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from semantic_query_engine.governance.audit import (
    GENESIS,
    AuditLog,
    AuditRecord,
    audit_enabled,
    verify_chain,
)
from semantic_query_engine.governance.principals import STEWARD, Principal


def _record(run_id: str, **overrides) -> AuditRecord:
    defaults = dict(
        run_id=run_id,
        timestamp="2026-09-21T10:00:00.000+00:00",
        domain="retail",
        principal="steward",
        role="data_steward",
        question="total revenue",
        sql="SELECT 1 LIMIT 1000",
        outcome="answer",
        row_count=1,
        elapsed_ms=12.5,
    )
    return AuditRecord(**{**defaults, **overrides})


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / "audit.jsonl")


# ---------------------------------------------------------------------------
# Appending
# ---------------------------------------------------------------------------


def test_the_first_record_chains_from_the_genesis_anchor(log):
    """Without an anchor, a first record has nothing to be checked against and a
    log truncated to one forged row would verify cleanly."""
    entry = log.append(_record("a"))
    assert entry["prev_hash"] == GENESIS
    assert verify_chain(log.read()) == []


def test_records_accumulate_rather_than_replace(log):
    for name in "abc":
        log.append(_record(name))
    assert [record["run_id"] for record in log.read()] == ["a", "b", "c"]
    assert verify_chain(log.read()) == []


def test_each_record_chains_to_the_one_before_it(log):
    first = log.append(_record("a"))
    second = log.append(_record("b"))
    assert second["prev_hash"] == first["hash"]


def test_the_chain_survives_a_second_writer(log, tmp_path):
    """The last hash is re-read from the file per append, not cached in memory:
    two processes logging to one file must produce one chain, not two forks."""
    other = AuditLog(tmp_path / "audit.jsonl")
    log.append(_record("a"))
    other.append(_record("b"))
    log.append(_record("c"))
    assert verify_chain(log.read()) == []


def test_reading_an_absent_log_is_empty_not_an_error(tmp_path):
    assert AuditLog(tmp_path / "nothing.jsonl").read() == []


def test_limit_returns_the_most_recent_records(log):
    for name in "abcde":
        log.append(_record(name))
    assert [r["run_id"] for r in log.read(limit=2)] == ["d", "e"]


# ---------------------------------------------------------------------------
# Tamper evidence
# ---------------------------------------------------------------------------


def test_editing_a_record_is_detected(log):
    """The case the chain exists for: somebody quietly changes what a query did."""
    log.append(_record("a", principal="analyst_north"))
    log.append(_record("b"))

    records = log.read()
    records[0]["principal"] = "steward"
    log.path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    problems = verify_chain(log.read())
    assert any("was edited" in problem for problem in problems)


def test_deleting_a_record_is_detected(log):
    """A deletion breaks the *link* without breaking any record's own hash, which
    is why both checks are made and both are reported."""
    for name in "abc":
        log.append(_record(name))
    records = log.read()
    del records[1]
    log.path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    problems = verify_chain(log.read())
    assert any("chain break" in problem for problem in problems)


def test_a_wholesale_rewrite_is_not_detected(log, tmp_path):
    """Stated as a test because the honest claim is tamper-*evident*, not
    tamper-proof: anyone who can write the file can rebuild the chain. Shipping
    the head hash off-box is what would close this, and that is a deployment
    concern. A test asserting the opposite would be the security theatre the
    module docstring argues against.
    """
    log.append(_record("a", principal="analyst_north"))
    rebuilt = AuditLog(tmp_path / "rebuilt.jsonl")
    rebuilt.append(_record("a", principal="steward"))
    assert verify_chain(rebuilt.read()) == []


def test_a_truncated_line_is_reported_rather_than_crashing_the_reader(log):
    """A process killed mid-write leaves half a line. The moment somebody needs to
    read an audit log is exactly the moment it may be damaged, so one bad line
    must not make the rest unreadable -- and must not pass unnoticed either."""
    log.append(_record("a"))
    with open(log.path, "a", encoding="utf-8") as handle:
        handle.write('{"run_id": "b", "half\n')
    log.append(_record("c"))

    records = log.read()
    assert len(records) == 3
    assert "unparseable" in records[1]
    assert verify_chain(records) != []


def test_the_hash_does_not_depend_on_key_order(log):
    """Re-verifying a log written by another process must not fail for no reason."""
    entry = log.append(_record("a"))
    shuffled = dict(reversed(list(entry.items())))
    assert verify_chain([shuffled]) == []


# ---------------------------------------------------------------------------
# What gets logged
# ---------------------------------------------------------------------------


@pytest.fixture
def audited(tmp_path, monkeypatch):
    """A pipeline whose audit log is a throwaway file, with auditing back on."""
    from semantic_query_engine.core.domains import active_domain
    from semantic_query_engine.pipeline.orchestrator import AnalyticsPipeline

    monkeypatch.setenv("SQE_AUDIT", "1")
    domain = dataclasses.replace(active_domain(), audit_log_path=tmp_path / "run.jsonl")
    monkeypatch.setattr("semantic_query_engine.core.domains._active", domain)
    return AnalyticsPipeline(), AuditLog(tmp_path / "run.jsonl")


def test_a_successful_run_is_logged(audited):
    pipeline, log = audited
    result = pipeline.run("What is total revenue?")
    records = log.read()
    assert len(records) == 1
    assert records[0]["outcome"] == result.to_dict().get("reason") or records[0]["outcome"] == "answer"
    assert records[0]["principal"] == "steward"
    assert verify_chain(records) == []


def test_a_run_that_produced_no_answer_is_logged_too(audited):
    """The rows an audit reader wants are the refusals, not the answers."""
    pipeline, log = audited
    pipeline.run("how are things")
    records = log.read()
    assert len(records) == 1
    assert records[0]["outcome"] in {"clarification", "validation_failed", "no_data", "execution_failed"}


def test_the_logged_sql_is_the_sql_that_executed(audited):
    """On a governed run the model's SQL and the executed SQL differ, and only one
    of them is what the warehouse saw."""
    pipeline, log = audited
    principal = Principal(id="north", role="regional_analyst", grants={"region": ("PL-North",)})
    pipeline.run("What is total revenue by region?", principal=principal)

    record = log.read()[-1]
    assert record["principal"] == "north"
    if record["outcome"] == "answer":
        assert "PL-North" in record["sql"]
        assert record["applied_policies"]


def test_the_run_id_ties_the_record_to_the_trace(audited):
    pipeline, log = audited
    _, state = pipeline.run_traced("What is total revenue?", principal=STEWARD)
    assert log.read()[-1]["run_id"] == state.run_id


def test_auditing_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SQE_AUDIT", "0")
    assert not audit_enabled()
    monkeypatch.setenv("SQE_AUDIT", "1")
    assert audit_enabled()
