"""The semantic cache, and the rule that keeps it from inventing answers.

A cache that returns a previous question's SQL is a confidently-wrong-answer
generator unless something stops "revenue in 2023" matching "revenue in 2024".
These tests exist mostly to pin that something down; the hit-rate arithmetic is
the easy half.

Everything here runs on the lexical backend -- no provider, no network -- which
is also the mode a local-model checkout of this repo actually runs in.
"""

from __future__ import annotations

import json

from semantic_query_engine.core.semantic_cache import (
    LEXICAL_THRESHOLD,
    SemanticCache,
    entity_key,
    lexical_vector,
    question_literals,
    sparse_cosine,
)


def cache(tmp_path, **kwargs) -> SemanticCache:
    return SemanticCache(path=tmp_path / "cache.json", **kwargs)


# ---------------------------------------------------------------------------
# Rule 2: entities are a key, not a similarity
# ---------------------------------------------------------------------------


def test_the_same_phrasing_with_a_different_year_is_never_a_hit(tmp_path):
    """The failure the entity key exists to prevent, asserted directly.

    These two questions differ by one character. Any similarity measure worth
    using scores them as near-identical -- which is exactly why similarity must
    not be what decides. Serving 2023's SQL for a 2024 question produces a
    plausible, precise, wrong number with no model call to blame.
    """
    store = cache(tmp_path)
    store.store(
        "What was total revenue in 2023?",
        "SELECT SUM(units_sold * price_unit) FROM fmcg_sales WHERE year = 2023",
        {"timeframe": "2023"},
    )

    similarity = sparse_cosine(
        lexical_vector("What was total revenue in 2023?"),
        lexical_vector("What was total revenue in 2024?"),
    )
    assert similarity > 0.8, "the two questions are near-identical, as assumed"

    assert store.lookup("What was total revenue in 2024?", {"timeframe": "2024"}) is None
    assert store.stats.entity_blocked == 1


def test_a_different_dimension_value_is_never_a_hit(tmp_path):
    store = cache(tmp_path)
    store.store("Revenue for the Milk category", "SELECT 1", {"category": "Milk"})
    assert store.lookup("Revenue for the Juice category", {"category": "Juice"}) is None


def test_rephrasing_the_same_question_is_a_hit(tmp_path):
    """What the cache is actually for: one question, many phrasings."""
    store = cache(tmp_path, threshold=0.5)
    store.store("Compare total revenue across all brands", "SELECT 1", {})

    hit = store.lookup("compare the total revenue across all brands", {})
    assert hit is not None
    assert hit.sql == "SELECT 1"
    assert hit.backend == "lexical"


def test_the_year_comes_from_the_question_not_from_the_planner(tmp_path):
    """The guard cannot rest on the planner's timeframe, because it is coarse.

    The planner reports both "revenue in 2023" and "revenue in 2024" as the
    same label, ``year_detected`` -- so an entity key built from its output
    alone gives the two questions an identical key, and the only thing left
    between a 2023 question and a 2024 answer is cosine similarity. Measured on
    a real run, those two questions embed at 0.96: above any threshold that
    still allows a genuine rephrasing through. The literal has to be read out of
    the question text, and this is the test that says so.
    """
    planner_entities = {"timeframe": "year_detected", "metric": "units_or_revenue"}
    assert entity_key(planner_entities, "total revenue in 2023") != entity_key(
        planner_entities, "total revenue in 2024"
    )

    store = cache(tmp_path, threshold=0.5)
    store.store("What was total revenue in 2023?", "SELECT 1", planner_entities)
    assert store.lookup("What was total revenue in 2024?", planner_entities) is None
    # ...and the same year, reworded, still hits. Only lightly reworded: this is
    # the lexical backend, which cannot see that "tell me the total revenue for
    # 2023" is the same question. Against a real embedding model that rephrasing
    # scored 0.959 and did hit -- which is the whole reason the two backends are
    # reported separately rather than under one hit rate.
    assert store.lookup("what was the total revenue in 2023", planner_entities) is not None


def test_a_different_top_n_is_a_different_question(tmp_path):
    """"Top 5 brands" and "top 10 brands" differ by one token and one answer."""
    store = cache(tmp_path, threshold=0.5)
    store.store("Show the top 5 brands by revenue", "SELECT 1", {})
    assert store.lookup("Show the top 10 brands by revenue", {}) is None


def test_literals_are_order_independent(tmp_path):
    """"2023 vs 2024" and "2024 vs 2023" are the same comparison."""
    assert question_literals("compare 2023 vs 2024") == question_literals("compare 2024 vs 2023")


def test_a_month_changes_the_key(tmp_path):
    assert entity_key({}, "revenue in March 2024") != entity_key({}, "revenue in April 2024")


def test_the_metric_family_is_not_part_of_the_key(tmp_path):
    """``metric`` is the planner's coarse label, not a literal in a filter.

    Including it would split "total revenue" from "revenue" -- two spellings of
    one question -- for no protection, because nothing in the SQL depends on it
    that is not already pinned by the dimension values and the timeframe.
    """
    assert entity_key({"metric": "units_or_revenue"}) == entity_key({})
    assert entity_key({"region": "PL-North"}) != entity_key({})


def test_entity_order_does_not_change_the_key(tmp_path):
    assert entity_key({"region": "PL-North", "category": "Milk"}) == entity_key(
        {"category": "Milk", "region": "PL-North"}
    )


# ---------------------------------------------------------------------------
# Similarity behaviour
# ---------------------------------------------------------------------------


def test_an_unrelated_question_with_no_entities_is_not_a_hit(tmp_path):
    """The empty entity key matches everything, so similarity has to carry it."""
    store = cache(tmp_path)
    store.store("Show monthly revenue trend", "SELECT 1", {})
    assert store.lookup("What is the stock depletion rate", {}) is None


def test_word_order_is_not_free(tmp_path):
    """Bigrams are why. On unigrams alone these two vectors are identical."""
    forward = lexical_vector("revenue by region")
    backward = lexical_vector("region by revenue")
    assert sparse_cosine(forward, backward) < LEXICAL_THRESHOLD


# ---------------------------------------------------------------------------
# Rule 3, persistence, and the saving
# ---------------------------------------------------------------------------


def test_empty_sql_is_never_stored(tmp_path):
    """A failed generation is not a cheaper way to fail next time."""
    store = cache(tmp_path)
    store.store("anything", "   ", {})
    assert len(store) == 0


def test_a_hit_reports_the_tokens_the_original_generation_cost(tmp_path):
    """The saving is measured, not estimated: it is what the first call cost."""
    store = cache(tmp_path, threshold=0.5)
    store.store(
        "Compare total revenue across all brands",
        "SELECT 1",
        {},
        prompt_tokens=1800,
        completion_tokens=120,
        cost_usd=0.0021,
    )
    hit = store.lookup("compare the total revenue across all brands", {})
    assert hit is not None
    assert hit.saved_prompt_tokens == 1800
    assert store.stats.saved_completion_tokens == 120
    assert store.stats.saved_cost_usd == 0.0021
    assert store.stats.hit_rate == 1.0


def test_the_cache_survives_a_restart(tmp_path):
    store = cache(tmp_path, threshold=0.5)
    store.store("Compare total revenue across all brands", "SELECT 1", {})
    store.save()

    reopened = cache(tmp_path, threshold=0.5)
    assert len(reopened) == 1
    assert reopened.lookup("compare the total revenue across all brands", {}) is not None


def test_a_corrupt_cache_file_is_emptied_rather_than_fatal(tmp_path):
    """The cache holds no irreplaceable state, so it must never stop the engine.

    Refusing to answer questions because an optimisation's JSON file went bad
    would trade a slow pipeline for no pipeline.
    """
    path = tmp_path / "cache.json"
    path.write_text("{not json at all", encoding="utf-8")
    store = SemanticCache(path=path)
    assert len(store) == 0
    assert store.lookup("anything", {}) is None


def test_clear_empties_the_file_too(tmp_path):
    store = cache(tmp_path)
    store.store("a question", "SELECT 1", {})
    store.save()
    assert store.clear() == 1
    assert json.loads((tmp_path / "cache.json").read_text(encoding="utf-8")) == []


def test_re_asking_the_identical_question_does_not_duplicate_it(tmp_path):
    store = cache(tmp_path)
    store.store("Show monthly revenue trend", "SELECT 1", {}, prompt_tokens=900)
    store.store("Show monthly revenue trend", "SELECT 2", {}, prompt_tokens=0)
    assert len(store) == 1
    # The refreshed SQL wins, but the recorded cost does not go to zero: the
    # second call may itself have been a fallback or a hit, and forgetting what
    # the generation cost would erase the saving every later hit is credited.
    hit = store.lookup("Show monthly revenue trend", {})
    assert hit is not None
    assert hit.sql == "SELECT 2"
    assert hit.saved_prompt_tokens == 900


# ---------------------------------------------------------------------------
# The embedding backend
# ---------------------------------------------------------------------------


def test_an_embedding_failure_degrades_to_lexical_rather_than_raising(tmp_path):
    """The caller is trying to answer a question; the cache is the least
    important thing in that path."""

    def broken(_text: str) -> list[float]:
        raise RuntimeError("embeddings endpoint is down")

    store = SemanticCache(path=tmp_path / "cache.json", embedder=broken, threshold=0.5)
    store.store("Compare total revenue across all brands", "SELECT 1", {})
    hit = store.lookup("compare the total revenue across all brands", {})
    assert hit is not None
    assert hit.backend == "lexical"


def test_the_backend_that_served_a_hit_is_recorded(tmp_path):
    """"40% hit rate" means different things in the two modes, so both are counted."""

    def embed(text: str) -> list[float]:
        # Two near-parallel vectors: any two questions are "similar" here, which
        # is fine -- what is under test is that the semantic path is taken and
        # labelled, not the quality of a real embedding model.
        return [1.0, 0.1 if "brand" in text else 0.11]

    store = SemanticCache(path=tmp_path / "cache.json", embedder=embed, threshold=0.5)
    store.store("Compare total revenue across all brands", "SELECT 1", {})
    hit = store.lookup("a completely different sentence", {})
    assert hit is not None
    assert hit.backend == "semantic"
    assert store.stats.semantic_hits == 1
    assert store.stats.lexical_hits == 0
