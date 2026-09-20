"""Domain registries: the single source of truth for metrics, dimensions and identifiers."""

from __future__ import annotations

from semantic_query_engine.domain.registry import (
    DimensionRegistry,
    IdentifierRegistry,
    load_domain_registries,
)


def test_registries_load_as_a_named_bundle():
    registries = load_domain_registries()
    assert registries.metrics.all()
    assert registries.dimensions.dimensions()
    assert registries.identifiers.names() == ["sku", "store_id", "promo_id"]
    # The star schema's grain declarations travel in the same bundle: the
    # validator's fan-out check reads them from here, not from its own copy.
    assert registries.grains.grain("fmcg_sales").key_columns == {"date", "sku", "store_id"}


# ---------------------------------------------------------------------------
# IdentifierRegistry -- the SKU pattern used to be copied into the planner, the
# validator and a fallback template independently.
# ---------------------------------------------------------------------------


def test_identifier_is_found_regardless_of_the_case_the_user_typed():
    registry = IdentifierRegistry({"sku": r"\b([A-Z]{2}-\d{3})\b"})
    assert registry.find("sku", "units for SKU mi-006 please") == "MI-006"
    assert registry.find("sku", "What were total units sold for MI-006?") == "MI-006"


def test_identifier_returns_none_when_absent():
    registry = IdentifierRegistry({"sku": r"\b([A-Z]{2}-\d{3})\b"})
    assert registry.find("sku", "total revenue by region") is None


def test_unknown_identifier_name_is_not_an_error():
    registry = IdentifierRegistry({"sku": r"\b([A-Z]{2}-\d{3})\b"})
    assert registry.find("store", "anything") is None


def test_find_all_returns_only_the_identifiers_present():
    registry = IdentifierRegistry({"sku": r"\b([A-Z]{2}-\d{3})\b", "store": r"\bST-(\d{4})\b"})
    assert registry.find_all("SKU MI-006 at store ST-0042") == {"sku": "MI-006", "store": "0042"}
    assert registry.find_all("revenue by region") == {}


def test_the_planner_and_the_semantic_layer_agree_on_the_sku_pattern():
    """The planner must recognise exactly the identifiers the semantic layer declares."""
    from semantic_query_engine.agents.planner import PlannerAgent

    planner = PlannerAgent()
    plan = planner.run("What were total units sold for SKU MI-006 in January 2024?")
    assert plan.entities["sku"] == "MI-006"


# ---------------------------------------------------------------------------
# DimensionRegistry.match_all -- the arity rule the validator depends on
# ---------------------------------------------------------------------------


def _region_registry() -> DimensionRegistry:
    return DimensionRegistry(
        values={"region": ["PL-North", "PL-South", "PL-Central"]},
        aliases={"region": {"north": "PL-North", "south": "PL-South", "central": "PL-Central"}},
    )


def test_match_all_finds_every_named_value():
    registry = _region_registry()
    assert set(registry.match_all("region", "compare the north versus the south")) == {
        "PL-North",
        "PL-South",
    }


def test_match_all_deduplicates_an_alias_and_its_canonical_form():
    registry = _region_registry()
    assert registry.match_all("region", "south and pl-south") == ["PL-South"]


def test_match_all_is_empty_when_no_value_is_named():
    registry = _region_registry()
    assert registry.match_all("region", "total revenue by region") == []


def test_match_returns_a_single_value_where_match_all_returns_the_set():
    """The planner routes on one value; the validator needs to know how many were
    named, because two means a comparison rather than a filter."""
    registry = _region_registry()
    text = "compare the north versus the south"
    assert registry.match("region", text) in {"PL-North", "PL-South"}
    assert len(registry.match_all("region", text)) == 2
