"""Synthesis agent: deterministic fallback, and the LLM-primary path via a fake client."""

from __future__ import annotations

import pandas as pd
from fakes import FakeChatClient, RaisingChatClient

from semantic_query_engine.agents.synthesis import SynthesisAgent

DF = pd.DataFrame({"region": ["PL-South", "PL-North"], "total_revenue": [6666229.81, 6664220.52]})


def test_fallback_synthesis_picks_the_top_row_and_a_bar_chart():
    agent = SynthesisAgent()
    response = agent.run("What is total revenue by region?", "SELECT ...", DF, "descriptive_lookup", [])
    assert "PL-South" in response.narrative_summary
    assert response.chart_recommendation in {"bar", "grouped_bar", "line", "scatter", "pie"}


def test_llm_synthesis_uses_the_fake_client_and_parses_the_payload(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    fake = FakeChatClient(
        {
            "narrative_summary": "PL-South leads with the highest revenue.",
            "key_metric": "£6.67M -- PL-South",
            "comparison_context": "PL-South edges PL-North by £2.0K.",
            "chart_recommendation": "bar",
        }
    )
    agent = SynthesisAgent(client_factory=lambda settings: fake)

    response = agent.run("What is total revenue by region?", "SELECT ...", DF, "descriptive_lookup", [])

    assert response.narrative_summary == "PL-South leads with the highest revenue."
    assert response.chart_recommendation == "bar"
    assert fake.calls


def test_llm_synthesis_coerces_an_unrecognised_chart_type_to_bar(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    fake = FakeChatClient({"narrative_summary": "Some summary.", "chart_recommendation": "sunburst"})
    agent = SynthesisAgent(client_factory=lambda settings: fake)

    response = agent.run("q", "SELECT ...", DF, "descriptive_lookup", [])

    assert response.chart_recommendation == "bar"


def test_llm_synthesis_falls_back_on_a_provider_error(monkeypatch):
    monkeypatch.setenv("SQE_LLM_API_KEY", "sk-test-placeholder-not-a-real-key")
    agent = SynthesisAgent(client_factory=lambda settings: RaisingChatClient())

    response = agent.run("What is total revenue by region?", "SELECT ...", DF, "descriptive_lookup", [])

    assert "PL-South" in response.narrative_summary
