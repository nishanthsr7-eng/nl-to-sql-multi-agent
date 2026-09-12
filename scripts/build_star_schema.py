"""Generate the Phase 4 star schema from the original denormalized sales CSV.

Why generate rather than hand-author: every table below has to stay *consistent*
with the 190k fact rows that already exist, because the Phase 3 gold set scores
against numbers those rows produce. A hand-built dimension drifts the first time
someone edits it; a seeded generator re-derives the whole schema from the one
source file and is reproducible byte-for-byte.

The load-bearing property, and the reason store allocation works the way it does:
**no existing aggregate changes.** ``fmcg_sales`` is unique on
(date, sku, region, channel), and each row is assigned exactly one store drawn
from the stores belonging to its own region and channel. Adding a column that is
unique per row cannot change any SUM, so every pre-Phase-4 gold case keeps its
answer, and any accuracy drop measured after this lands is attributable to the
joins the model now has to write -- not to the data moving underneath it.

Run:  python scripts/build_star_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE_CSV = ROOT / "data" / "raw" / "fmcg_daily_sales_2022_2024.csv"
OUT_DIR = ROOT / "data" / "raw" / "star"

# A fixed seed is the difference between "generated data" and "arbitrary data".
# Re-running this script must reproduce the warehouse the reported numbers were
# measured against, or the eval timeline in evals/results/ compares two runs
# against two different worlds.
SEED = 20260919

# fact_inventory is dense -- one row per store x sku x day -- so it is bounded to
# a trailing window rather than the full three years. 90 days over the active
# store-sku pairs is the same order of magnitude as the sales fact, which keeps
# the repo checkout honest. It is also how a real inventory snapshot table
# behaves: you keep the recent position, not three years of daily stock levels.
INVENTORY_WINDOW_DAYS = 90

CITIES_BY_REGION: dict[str, list[tuple[str, str]]] = {
    "PL-North": [("Gdansk", "tier_1"), ("Szczecin", "tier_2"), ("Olsztyn", "tier_3")],
    "PL-South": [("Krakow", "tier_1"), ("Katowice", "tier_2"), ("Rzeszow", "tier_3")],
    "PL-Central": [("Warszawa", "tier_1"), ("Lodz", "tier_2"), ("Radom", "tier_3")],
}

# Store format is a property of the channel, not a free dimension: an E-commerce
# "store" is a fulfilment node, and a Discount store has no hypermarket variant.
FORMATS_BY_CHANNEL: dict[str, list[str]] = {
    "Retail": ["Hypermarket", "Supermarket", "Convenience"],
    "Discount": ["DiscountStore"],
    "E-commerce": ["FulfilmentCentre"],
}

STORES_PER_COMBINATION = 7

PROMO_MECHANICS = ["BOGO", "PriceOff", "Bundle", "LoyaltyPoints", "DisplayFeature"]


def build_dim_store(sales: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """One row per physical store. Stores are nested inside (region, channel)."""
    rows = []
    store_seq = 0
    for region in sorted(sales["region"].unique()):
        for channel in sorted(sales["channel"].unique()):
            formats = FORMATS_BY_CHANNEL[channel]
            cities = CITIES_BY_REGION[region]
            for i in range(STORES_PER_COMBINATION):
                store_seq += 1
                city, tier = cities[i % len(cities)]
                rows.append(
                    {
                        "store_id": f"ST-{store_seq:03d}",
                        "region": region,
                        "channel": channel,
                        "store_format": formats[i % len(formats)],
                        "city": city,
                        "population_tier": tier,
                        "opened_date": pd.Timestamp("2018-01-01")
                        + pd.Timedelta(days=int(rng.integers(0, 1400))),
                    }
                )
    return pd.DataFrame(rows)


def allocate_stores(
    sales: pd.DataFrame, stores: pd.DataFrame, rng: np.random.Generator
) -> pd.Series:
    """Assign every sales row a store from its own region and channel.

    Drawn per row rather than per (sku, region, channel) group so that a single
    store does not own a SKU outright -- that would make store_id functionally
    determine both region and sku, and the join would be trivially skippable.
    """
    by_combo = {
        (region, channel): group["store_id"].to_numpy()
        for (region, channel), group in stores.groupby(["region", "channel"])
    }
    allocated = np.empty(len(sales), dtype=object)
    for (region, channel), index in sales.groupby(["region", "channel"]).groups.items():
        candidates = by_combo[(region, channel)]
        positions = sales.index.get_indexer(index)
        allocated[positions] = rng.choice(candidates, size=len(positions))
    return pd.Series(allocated, index=sales.index, name="store_id")


def build_dim_product(sales: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """One row per SKU.

    ``pack_type`` is deliberately NOT here: the source data carries up to three
    pack types for the same SKU, so it is an attribute of the transaction, not of
    the product. Moving it into the dimension would mean inventing a fact the
    data does not support, and every pack_type gold case would start scoring
    against fiction.
    """
    grouped = sales.groupby("sku")
    product = grouped.agg(
        brand=("brand", "first"),
        category=("category", "first"),
        segment=("segment", "first"),
        launch_date=("date", "min"),
        median_price=("price_unit", "median"),
    ).reset_index()
    # Cost sits at 55-75% of the typical shelf price, so a margin metric has real
    # variance across SKUs rather than being a constant multiple of revenue.
    margin_factor = rng.uniform(0.55, 0.75, size=len(product))
    product["unit_cost"] = (product["median_price"] * margin_factor).round(2)
    return product.drop(columns=["median_price"])


def build_dim_calendar(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """One row per calendar day, and the only legitimate bridge to weekly data.

    ``week_start`` is Monday-aligned to match ``weekly_modeling_data.week``.
    Joining a daily fact to the weekly table on ``date = week`` matches only
    Mondays and silently drops six sevenths of the data; the supported path is
    daily -> dim_calendar.week_start -> weekly.week.
    """
    dates = pd.date_range(start, end, freq="D")
    cal = pd.DataFrame({"date": dates})
    iso = cal["date"].dt.isocalendar()
    cal["week_start"] = cal["date"] - pd.to_timedelta(cal["date"].dt.dayofweek, unit="D")
    cal["iso_week"] = iso["week"].astype(int)
    cal["year"] = cal["date"].dt.year
    cal["month"] = cal["date"].dt.month
    cal["day_of_week"] = cal["date"].dt.dayofweek + 1
    # A fiscal year starting in July is the point: it makes "Q1" ambiguous between
    # calendar and fiscal, which is exactly the kind of question the semantic
    # layer should disambiguate rather than the model guessing.
    cal["fiscal_year"] = np.where(cal["month"] >= 7, cal["year"] + 1, cal["year"])
    fiscal_month = ((cal["month"] - 7) % 12) + 1
    cal["fiscal_quarter"] = ((fiscal_month - 1) // 3) + 1
    cal["fiscal_week"] = (((cal["date"].dt.dayofyear + 181) % 365) // 7) + 1
    cal["season"] = np.select(
        [cal["month"].isin([12, 1, 2]), cal["month"].isin([6, 7, 8])],
        ["Winter", "Summer"],
        default="Shoulder",
    )
    holidays = {
        (1, 1), (1, 6), (5, 1), (5, 3), (8, 15),
        (11, 1), (11, 11), (12, 24), (12, 25), (12, 26),
    }
    cal["is_holiday"] = [
        (month, day) in holidays
        for month, day in zip(cal["date"].dt.month, cal["date"].dt.day, strict=True)
    ]
    cal["is_holiday_peak"] = cal["month"].isin([11, 12]) & (cal["iso_week"] >= 46)
    return cal


def build_fact_inventory(sales: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Daily stock position per store and SKU over a trailing window.

    Carries its own ``units_sold`` at the *same* grain as ``fmcg_sales``. That is
    the trap: joining sales to inventory on (date, sku, store_id) is grain-safe,
    but joining on (sku, date) alone fans out across stores, and summing
    ``units_sold`` from both tables double-counts. The validator grain check is
    what has to catch it.
    """
    end = sales["date"].max()
    start = end - pd.Timedelta(days=INVENTORY_WINDOW_DAYS - 1)
    window = sales[sales["date"].between(start, end)]

    pairs = window[["store_id", "sku"]].drop_duplicates()
    days = pd.DataFrame({"date": pd.date_range(start, end, freq="D")})
    dense = pairs.merge(days, how="cross")

    observed = window.groupby(["store_id", "sku", "date"], as_index=False).agg(
        units_sold=("units_sold", "sum"),
        receipts=("delivered_qty", "sum"),
    )
    inv = dense.merge(observed, on=["store_id", "sku", "date"], how="left")
    inv[["units_sold", "receipts"]] = inv[["units_sold", "receipts"]].fillna(0).astype(int)
    inv = inv.sort_values(["store_id", "sku", "date"]).reset_index(drop=True)

    seed_by_pair = dict(
        zip(
            (tuple(row) for row in pairs.to_numpy()),
            rng.integers(40, 400, size=len(pairs)),
            strict=True,
        )
    )

    # Stock is a running level, not a flow: closing = opening + receipts - sold,
    # and the next opening is the previous closing. Generating it as independent
    # random numbers per day would make SUM(closing_stock) across dates look
    # meaningful when summing a level across time never is.
    opening = np.empty(len(inv), dtype=int)
    closing = np.empty(len(inv), dtype=int)
    cursor = 0
    for pair, group in inv.groupby(["store_id", "sku"], sort=False):
        level = int(seed_by_pair[pair])
        for offset, (sold, received) in enumerate(
            zip(group["units_sold"].to_numpy(), group["receipts"].to_numpy(), strict=True)
        ):
            opening[cursor + offset] = level
            level = max(0, level + int(received) - int(sold))
            closing[cursor + offset] = level
        cursor += len(group)

    inv["opening_stock"] = opening
    inv["closing_stock"] = closing
    return inv[
        ["date", "store_id", "sku", "opening_stock", "receipts", "units_sold", "closing_stock"]
    ]


def build_fact_promotions(sales: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Promotion periods as date *ranges*, derived from contiguous promoted days.

    A range join, not an equi-join. A fourteen-day promo joined to daily sales on
    ``sku`` alone multiplies every matching sales row by fourteen -- the sharpest
    fan-out case in the schema, and one that executes perfectly and returns a
    plausible wrong number.
    """
    promoted = sales[sales["promotion_flag"] == 1][["sku", "region", "date"]]
    promoted = promoted.drop_duplicates().sort_values(["sku", "region", "date"])
    promoted = promoted.reset_index(drop=True)

    # A gap of more than one day between promoted dates starts a new promotion.
    gap = promoted.groupby(["sku", "region"])["date"].diff().dt.days
    promoted["run_id"] = ((gap.isna()) | (gap > 1)).cumsum()

    runs = promoted.groupby(["sku", "region", "run_id"], as_index=False).agg(
        start_date=("date", "min"), end_date=("date", "max")
    )
    runs = runs[runs["start_date"] != runs["end_date"]].reset_index(drop=True)

    runs["promo_id"] = [f"PR-{i + 1:05d}" for i in range(len(runs))]
    runs["discount_depth"] = rng.uniform(0.05, 0.35, size=len(runs)).round(3)
    runs["mechanic"] = rng.choice(PROMO_MECHANICS, size=len(runs))
    # Chain-wide promotions carry a NULL store_id. That nullable FK is the other
    # half of the trap: an inner join to dim_store silently drops every one of
    # them, and the total still looks plausible.
    runs["store_id"] = None
    return runs[
        [
            "promo_id", "sku", "region", "store_id",
            "start_date", "end_date", "discount_depth", "mechanic",
        ]
    ]


def main() -> int:
    if not SOURCE_CSV.exists():
        print(f"Source CSV missing: {SOURCE_CSV}", file=sys.stderr)
        return 1

    rng = np.random.default_rng(SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sales = pd.read_csv(SOURCE_CSV, parse_dates=["date"])
    sales = sales.sort_values(["date", "sku", "region", "channel"]).reset_index(drop=True)

    stores = build_dim_store(sales, rng)
    sales["store_id"] = allocate_stores(sales, stores, rng)

    duplicated = int(sales.duplicated(["date", "sku", "store_id"]).sum())
    if duplicated:
        # The declared grain of fmcg_sales becomes (date, sku, store_id), and the
        # validator fan-out check trusts that declaration. If allocation ever
        # broke it, every grain verdict downstream would be wrong -- so this is a
        # hard failure, not a warning.
        print(
            f"Store allocation broke the fact grain: {duplicated} duplicate keys",
            file=sys.stderr,
        )
        return 1

    products = build_dim_product(sales, rng)
    calendar = build_dim_calendar(pd.Timestamp("2022-01-01"), pd.Timestamp("2024-12-31"))
    inventory = build_fact_inventory(sales, rng)
    promotions = build_fact_promotions(sales, rng)

    # The narrow fact: descriptive attributes that belong to a dimension are
    # removed, so a question about category, brand or region can no longer be
    # answered without a join. pack_type stays because it varies within a SKU.
    narrow = sales[
        [
            "date", "sku", "store_id", "pack_type", "price_unit", "promotion_flag",
            "delivery_days", "stock_available", "delivered_qty", "units_sold",
        ]
    ]

    outputs = {
        "fmcg_sales.csv": narrow,
        "dim_product.csv": products,
        "dim_store.csv": stores,
        "dim_calendar.csv": calendar,
        "fact_inventory.csv": inventory,
        "fact_promotions.csv": promotions,
    }
    for name, frame in outputs.items():
        path = OUT_DIR / name
        frame.to_csv(path, index=False, date_format="%Y-%m-%d")
        print(f"{name:22} {len(frame):>9,} rows  {path.stat().st_size / 1e6:>6.1f} MB")

    revenue_before = float((sales["units_sold"] * sales["price_unit"]).sum())
    revenue_after = float((narrow["units_sold"] * narrow["price_unit"]).sum())
    print(f"\nRevenue invariant: {revenue_before:,.2f} -> {revenue_after:,.2f}")
    return 0 if abs(revenue_before - revenue_after) < 0.01 else 1


if __name__ == "__main__":
    raise SystemExit(main())
