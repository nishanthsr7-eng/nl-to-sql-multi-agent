"""Author the airline domain's semantic layer.

Written as a script rather than by hand for one reason: the dimension values
must be exactly what the generated CSVs contain. A hand-typed list drifts the
first time the generator changes, and the planner would then fail to recognise
a carrier that exists (or recognise one that does not), which reads as a model
failure in the eval report.
"""

import json
from pathlib import Path

import pandas as pd

ROOT = Path('.')
RAW = ROOT / 'data' / 'raw' / 'airline'
OUT = ROOT / 'data' / 'domains' / 'airline' / 'semantic_layer.json'

airport = pd.read_csv(RAW / 'dim_airport.csv', keep_default_na=False)
carrier = pd.read_csv(RAW / 'dim_carrier.csv', keep_default_na=False)
aircraft = pd.read_csv(RAW / 'dim_aircraft.csv', keep_default_na=False)
date = pd.read_csv(RAW / 'dim_date.csv', keep_default_na=False)
crew = pd.read_csv(RAW / 'dim_crew.csv', keep_default_na=False)


def col(name, type_, description, role=None):
    entry = {"name": name, "type": type_, "description": description}
    if role:
        entry["role"] = role
    return entry


tables = [
    {
        "table_name": "fact_flights",
        "description": (
            "Flight departures, one row per flight date and flight number. Carrier, airport "
            "and aircraft attributes live in the dimensions -- a question about alliance, "
            "region or aircraft type requires a join. KNOWN DEFECTS: arrival_delay_minutes "
            "is NULL on about 1.5% of operated flights the feed did not report, and a "
            "handful of rows carry a 2019 flight_date outside the 2023-2024 period this "
            "warehouse covers."
        ),
        "grain": {
            "columns": ["flight_date", "flight_number"],
            "additive_measures": ["seats", "passengers", "cancelled_flag"],
            "level_measures": ["fare_eur", "departure_delay_minutes", "arrival_delay_minutes"],
        },
        "columns": [
            col(
                "flight_date",
                "DATE",
                "Date of the scheduled departure. The warehouse covers 2023-01-01 to "
                "2024-12-31; a few feed-error rows fall outside it and have no dim_date "
                "row, so an unbounded aggregate disagrees with one joined to the calendar.",
                "day",
            ),
            col("flight_number", "VARCHAR", "Carrier-prefixed flight number, unique within a date"),
            col("carrier_code", "VARCHAR", "Operating carrier; joins to dim_carrier.carrier_code"),
            col("tail_number", "VARCHAR", "Airframe operating the flight; joins to dim_aircraft.tail_number"),
            col("origin_airport", "VARCHAR", "Departure airport; joins to dim_airport.airport_code"),
            col("destination_airport", "VARCHAR", "Arrival airport; joins to dim_airport.airport_code"),
            col(
                "distance_km",
                "INTEGER",
                "Great-circle route distance in kilometres. A property of the route, identical on every flight of "
                "it.",
            ),
            col(
                "seats",
                "INTEGER",
                "Seats offered for sale on this departure. Not the airframe's capacity -- that is "
                "dim_aircraft.seat_capacity.",
            ),
            col("passengers", "INTEGER", "Passengers who boarded"),
            col(
                "fare_eur",
                "FLOAT",
                "Average fare paid per passenger on this departure, in EUR. A rate, not a total: do not SUM it.",
            ),
            col("departure_delay_minutes", "INTEGER", "Minutes late leaving the gate; negative means early"),
            col(
                "arrival_delay_minutes",
                "INTEGER",
                "Minutes late at the arrival gate; negative means early. A flight is on time at 15 minutes or less.",
            ),
            col(
                "cancelled_flag",
                "INTEGER",
                "1 if the flight was cancelled, 0 otherwise. A cancelled flight carries no passengers and no delay.",
            ),
        ],
    },
    {
        "table_name": "fact_fuel",
        "description": (
            "Fuel uplift, one row per fuel date and tail number -- NOT per flight. An airframe "
            "flies several times a day, so joining this to fact_flights on the tail and the "
            "date multiplies every flight row by that day's fuel record."
        ),
        "grain": {
            "columns": ["fuel_date", "tail_number"],
            "additive_measures": ["fuel_litres", "fuel_cost_eur"],
            "level_measures": [],
        },
        "columns": [
            col("fuel_date", "DATE", "Date the fuel was uplifted", "day"),
            col("tail_number", "VARCHAR", "Airframe fuelled; joins to dim_aircraft.tail_number"),
            col("fuel_litres", "INTEGER", "Litres uplifted across the whole day for this airframe"),
            col("fuel_cost_eur", "FLOAT", "Cost of that uplift in EUR"),
        ],
    },
    {
        "table_name": "dim_carrier",
        "description": "Operating carriers, one row per carrier code.",
        "grain": {"columns": ["carrier_code"], "additive_measures": [], "level_measures": []},
        "columns": [
            col("carrier_code", "VARCHAR", "Two-letter carrier code"),
            col("carrier_name", "VARCHAR", "Carrier's trading name"),
            col("alliance", "VARCHAR", "Alliance membership, or 'Unaligned' for a carrier in none"),
            col("service_model", "VARCHAR", "FullService, LowCost or Regional"),
        ],
    },
    {
        "table_name": "dim_airport",
        "description": "Airports, one row per airport code. Both the origin and the destination of a flight join here.",
        "grain": {"columns": ["airport_code"], "additive_measures": [], "level_measures": []},
        "columns": [
            col("airport_code", "VARCHAR", "Three-letter IATA-style airport code"),
            col("city", "VARCHAR", "City served"),
            col("country", "VARCHAR", "Country the airport is in"),
            col("region", "VARCHAR", "Geographic region grouping, e.g. Europe-West"),
            col("hub_size", "VARCHAR", "Congestion tier: mega_hub, large_hub, medium_hub or small_hub"),
        ],
    },
    {
        "table_name": "dim_aircraft",
        "description": (
            "Airframes, one row per tail number and validity window -- a slowly-changing "
            "dimension, NOT a lookup. Two tails changed operator mid-period and therefore "
            "have two rows each, so joining on tail_number alone multiplies those tails' "
            "flights. Join on the validity window as well: "
            "ON f.tail_number = ac.tail_number AND f.flight_date >= ac.valid_from "
            "AND f.flight_date < ac.valid_to."
        ),
        "grain": {
            "columns": ["tail_number", "valid_from"],
            "additive_measures": [],
            "level_measures": [],
            # Declaring the window is what lets the validator tell a correct
            # slowly-changing-dimension join (equality on the tail, range on the
            # window) from a fan-out (equality on the tail alone). Without it,
            # the only query that gets the right answer is also the only one the
            # guardrail rejects.
            "validity": {"from": "valid_from", "to": "valid_to"},
        },
        "columns": [
            col("tail_number", "VARCHAR", "Registration of the airframe"),
            col("carrier_code", "VARCHAR", "Carrier that operates this airframe"),
            col("aircraft_type", "VARCHAR", "Type designation, e.g. A320neo"),
            col("manufacturer", "VARCHAR", "Airbus, Boeing or Embraer"),
            col("body_type", "VARCHAR", "Narrowbody, Widebody or Regional"),
            col(
                "seat_capacity",
                "INTEGER",
                "Seats the airframe is configured for. Not the seats sold on a given flight -- that is "
                "fact_flights.seats.",
            ),
            col("year_built", "INTEGER", "Year the airframe entered service"),
            col(
                "valid_from",
                "DATE",
                "First date this row's attributes applied to the tail (inclusive).",
                "day",
            ),
            col(
                "valid_to",
                "DATE",
                "First date they no longer applied (exclusive); 9999-12-31 for the current "
                "row. Exclusive so the windows tile the period without overlapping -- a "
                "BETWEEN on them would match the changeover date twice.",
                "day",
            ),
        ],
    },
    {
        "table_name": "dim_date",
        "description": "Calendar attributes, one row per flight date.",
        "grain": {"columns": ["flight_date"], "additive_measures": [], "level_measures": []},
        "columns": [
            col("flight_date", "DATE", "The calendar date", "day"),
            col("day_of_week", "VARCHAR", "Weekday name"),
            col("is_weekend", "INTEGER", "1 on Saturday and Sunday"),
            col("fiscal_quarter", "VARCHAR", "Quarter label, e.g. 2024-Q3"),
            col("season", "VARCHAR", "Winter, Spring, Summer or Autumn"),
            col("is_holiday", "INTEGER", "1 on a public holiday that moves traffic"),
        ],
    },
    {
        "table_name": "dim_crew",
        "description": (
            "One row per crew member: who they fly for, what they fly as, and their contact "
            "details. The contact columns are personal data and are tagged as such in the "
            "governance block -- a caller without PII access sees them masked, and an "
            "expression computed over one is rejected rather than masked."
        ),
        "grain": {"columns": ["crew_id"], "additive_measures": [], "level_measures": []},
        "columns": [
            col(
                "crew_id",
                "VARCHAR",
                "Crew member identifier (primary key); pseudonymised for callers without PII access",
            ),
            col("carrier_code", "VARCHAR", "Carrier the crew member flies for; joins to dim_carrier.carrier_code"),
            col("crew_name", "VARCHAR", "Crew member's full name. Personal data."),
            col("crew_email", "VARCHAR", "Crew member's work email address. Personal data."),
            col("crew_phone", "VARCHAR", "Crew member's contact number. Personal data."),
            col("crew_role", "VARCHAR", "Captain, First Officer, Senior Cabin Crew or Cabin Crew"),
            col("base_airport", "VARCHAR", "Airport the crew member is based at"),
            col("hire_year", "INTEGER", "Year the crew member joined the carrier"),
        ],
    },
]

relationships = [
    {"from": "fact_flights.carrier_code", "to": "dim_carrier.carrier_code", "cardinality": "many_to_one"},
    {"from": "fact_flights.tail_number", "to": "dim_aircraft.tail_number", "cardinality": "many_to_one"},
    {"from": "fact_flights.origin_airport", "to": "dim_airport.airport_code", "cardinality": "many_to_one"},
    {"from": "fact_flights.destination_airport", "to": "dim_airport.airport_code", "cardinality": "many_to_one"},
    {"from": "fact_flights.flight_date", "to": "dim_date.flight_date", "cardinality": "many_to_one"},
    {"from": "fact_fuel.tail_number", "to": "dim_aircraft.tail_number", "cardinality": "many_to_one"},
    {"from": "fact_fuel.fuel_date", "to": "dim_date.flight_date", "cardinality": "many_to_one"},
    {"from": "dim_aircraft.carrier_code", "to": "dim_carrier.carrier_code", "cardinality": "many_to_one"},
    {"from": "dim_crew.carrier_code", "to": "dim_carrier.carrier_code", "cardinality": "many_to_one"},
]

# Row-level security and PII tags. Both are statements about what the data
# means -- a flight belongs to a carrier, an email address identifies a person
# -- so they live here rather than in a separate governance document that could
# disagree with this one about which tables exist.
#
# The carrier is the scoping dimension because it is the one an airline
# warehouse is actually partitioned by in practice: a carrier's analyst sees
# their own operation. Region would have been ambiguous -- a flight has two
# airports and therefore two regions -- and an ambiguous scope is the wrong
# thing to enforce silently.
governance = {
    "row_policies": [
        {
            "name": "carrier_scope",
            "grant_key": "carrier_code",
            "anchor_table": "dim_carrier",
            "anchor_column": "carrier_code",
            "description": (
                "A carrier analyst reads only their own carrier's flights, airframes and crew. "
                "fact_fuel carries no carrier code, so it is scoped through the airframe that "
                "burned the fuel -- the restriction holds whether or not the query joined "
                "dim_aircraft."
            ),
            "tables": [
                {"table": "fact_flights", "mode": "direct", "key": "carrier_code"},
                {"table": "dim_aircraft", "mode": "direct", "key": "carrier_code"},
                {"table": "dim_crew", "mode": "direct", "key": "carrier_code"},
                {
                    "table": "fact_fuel", "mode": "semijoin", "key": "tail_number",
                    "through": {"table": "dim_aircraft", "column": "tail_number", "filter_column": "carrier_code"},
                },
            ],
        }
    ],
    "pii": [
        {
            "table": "dim_crew", "column": "crew_name",
            "classification": "name", "mask": "redact",
            "description": "Identifies a person directly; there is no partial form of a name worth showing.",
        },
        {
            "table": "dim_crew", "column": "crew_email",
            "classification": "email", "mask": "email",
            "description": "The domain is operationally useful and not identifying; the local part is the identity.",
        },
        {
            "table": "dim_crew", "column": "crew_phone",
            "classification": "phone", "mask": "last4",
            "description": "The last four digits are what a person uses to recognise their own number.",
        },
        {
            "table": "dim_crew", "column": "crew_id",
            "classification": "pseudonymous_id", "mask": "hash",
            "description": (
                "Not identifying on its own, but it is the join key to every other column here, "
                "so it is stably pseudonymised: a report can still group by crew member without "
                "naming one."
            ),
        },
    ],
}

business_metrics = [
    {
        "metric_name": "on_time_rate",
        "definition": (
            "SUM(CASE WHEN arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0 "
            "/ COUNT(arrival_delay_minutes)"
        ),
        "description": (
            "Share of flights arriving within 15 minutes of schedule. Cancelled flights have no arrival and must be "
            "excluded with cancelled_flag = 0."
        ),
        "triggers": ["on time", "on-time", "punctual", "otp"],
    },
    {
        "metric_name": "load_factor",
        "definition": "SUM(passengers) * 1.0 / SUM(seats)",
        "description": (
            "Passengers carried as a share of seats offered. Both legs are additive, so this is a ratio of sums, "
            "never an average of per-flight ratios."
        ),
        "triggers": ["load factor", "occupancy", "seat fill"],
    },
    {
        "metric_name": "cancellation_rate",
        "definition": "SUM(cancelled_flag) * 1.0 / COUNT(*)",
        "description": "Share of scheduled departures that did not operate.",
        "triggers": ["cancellation", "cancelled", "cancel"],
    },
    {
        "metric_name": "passenger_revenue",
        "definition": "SUM(passengers * fare_eur)",
        "description": (
            "Ticket revenue. fare_eur is a per-passenger rate, so it is multiplied by passengers, never summed on "
            "its own."
        ),
        "triggers": ["revenue", "ticket sales", "takings"],
    },
    {
        "metric_name": "average_arrival_delay",
        "definition": "AVG(arrival_delay_minutes)",
        "description": (
            "Mean arrival delay in minutes over operated flights. Distinct from on_time_rate: a carrier can have a "
            "good average and poor punctuality."
        ),
        "triggers": ["delay", "late", "lateness"],
    },
    {
        "metric_name": "fuel_per_seat_km",
        "definition": "SUM(fuel_litres) * 1.0 / SUM(seats * distance_km)",
        "description": (
            "Litres per available seat-kilometre. Its two legs sit on tables at different grains -- fact_fuel is "
            "per tail-day, fact_flights per flight -- so it can only be computed by aggregating each side before "
            "joining."
        ),
        "triggers": ["fuel", "efficiency", "burn"],
    },
]

dimension_values = {
    "carrier_name": sorted(carrier["carrier_name"].unique().tolist()),
    "alliance": sorted(carrier["alliance"].unique().tolist()),
    "service_model": sorted(carrier["service_model"].unique().tolist()),
    "region": sorted(airport["region"].unique().tolist()),
    "country": sorted(airport["country"].unique().tolist()),
    "city": sorted(airport["city"].unique().tolist()),
    "hub_size": sorted(airport["hub_size"].unique().tolist()),
    "aircraft_type": sorted(aircraft["aircraft_type"].unique().tolist()),
    "manufacturer": sorted(aircraft["manufacturer"].unique().tolist()),
    "body_type": sorted(aircraft["body_type"].unique().tolist()),
    "season": sorted(date["season"].unique().tolist()),
    "day_of_week": sorted(date["day_of_week"].unique().tolist()),
}

dimension_aliases = {
    "alliance": {
        "skyway": "Skyway", "meridian": "Meridian",
        "unaligned": "Unaligned", "no alliance": "Unaligned", "independent": "Unaligned",
    },
    "service_model": {
        "low cost": "LowCost", "low-cost": "LowCost", "budget": "LowCost", "lowcost": "LowCost",
        "full service": "FullService", "full-service": "FullService", "legacy": "FullService",
        "regional": "Regional",
    },
    "region": {
        "western europe": "Europe-West", "west": "Europe-West", "europe-west": "Europe-West",
        "central europe": "Europe-Central", "europe-central": "Europe-Central",
        "southern europe": "Europe-South", "south": "Europe-South", "europe-south": "Europe-South",
        "northern europe": "Europe-North", "nordics": "Europe-North", "europe-north": "Europe-North",
    },
    "body_type": {"narrowbody": "Narrowbody", "narrow body": "Narrowbody",
                  "widebody": "Widebody", "wide body": "Widebody", "regional jet": "Regional"},
    "hub_size": {"mega hub": "mega_hub", "large hub": "large_hub",
                 "medium hub": "medium_hub", "small hub": "small_hub"},
    "manufacturer": {"airbus": "Airbus", "boeing": "Boeing", "embraer": "Embraer"},
}

# Airport codes are enumerated, not shaped. IdentifierRegistry applies every
# pattern case-insensitively -- identifiers are conventionally upper-case while
# questions are not -- so a shape-based \b[A-Z]{3}\b matches "the", "for" and
# "are": the planner read "What is the on-time rate..." as a question about
# airport THE. With twelve airports the alternation is both exact and cheap, and
# the shape-based patterns stay reserved for the genuinely high-cardinality
# identifiers (flight numbers, tail numbers) they were designed for.
identifier_patterns = {
    "flight_number": r"\b([A-Z]{2}\d{4})\b",
    "tail_number": r"\b([A-Z]{2}-\d{3})\b",
    "airport_code": r"\b(" + "|".join(sorted(airport["airport_code"])) + r")\b",
}

language = {
    "domain_noun": "airline operations",
    "flag_columns": [
        {"name": "cancelled_flag", "description": "INTEGER -- 1 (cancelled), 0 (operated)"},
        {"name": "is_holiday", "description": "INTEGER -- 1 (public holiday), 0 otherwise"},
        {"name": "is_weekend", "description": "INTEGER -- 1 (Saturday or Sunday), 0 otherwise"},
    ],
    "metric_keywords": {
        "punctuality": ["on time", "on-time", "punctual", "otp", "delay", "late", "lateness"],
        "load": ["load factor", "occupancy", "seat fill", "passengers", "seats"],
        "cancellation": ["cancellation", "cancelled", "cancel"],
        "revenue": ["revenue", "fare", "ticket sales", "takings"],
        "fuel": ["fuel", "burn", "efficiency"],
    },
    "anchor_dimensions": ["carrier_name", "aircraft_type", "city"],
    "scope_dimensions": ["carrier_name", "alliance", "service_model", "region", "aircraft_type", "city"],
    "grounding_keywords": [
        "on time", "delay", "load factor", "passengers", "seats", "cancellation",
        "revenue", "fuel", "carrier", "airline", "route", "aircraft",
    ],
    "vague_openers": ["how did", "how is", "tell me about", "what about"],
    "scoping_cues": [
        "compare", "versus", "growth", "highest", "lowest", "worst", "best",
        "which", "by", "across", "breakdown", "split",
    ],
    "strong_comparative_hints": ["compare", "vs", "versus", "growth", "rank", "highest", "lowest", "worst", "best"],
    "comparative_hints": [
        "compare", "vs", "versus", "growth", "delta",
        "month over month", "year over year", "yoy",
        "highest", "lowest", "top", "rank", "worst", "best",
        "more than", "less than", "better", "worse",
        "outperform", "underperform", "difference",
        "improved", "deteriorated", "increased", "decreased",
    ],
    "diagnostic_hints": [
        "which", "why", "driver", "drivers", "impact", "across", "join",
        "breakdown", "split", "by carrier", "by route", "by airport",
        "by aircraft", "by alliance", "by season", "hub", "congestion",
        "network", "contributed",
    ],
    "missing_param_prompts": {
        "metric": "metric (e.g. on-time rate, load factor, average delay)",
        "timeframe": "timeframe (e.g. last month, Q3 2024, 2023)",
        "scope": "network scope (e.g. carrier, route, airport, or aircraft type)",
    },
    "clarification_example": 'Example: "What was the on-time rate for LowCost carriers in 2024?"',
    "topic_prompts": [
        {
            "triggers": ["disruption", "disrupted", "irregular operations", "irrops"],
            "prompt": (
                "Your disruption question needs more detail. Would you like to see:\n"
                "• Punctuality — share of flights arriving within 15 minutes?\n"
                "• Cancellations — share of departures that did not operate?\n"
                "• Delay magnitude — average arrival delay in minutes?\n\n"
                "Please re-state with: metric, timeframe, and network scope."
            ),
        }
    ],
    "recovery_ideas": [
        {
            "triggers": ["fuel", "burn", "efficiency"],
            "suggestions": [
                "What is the fuel cost per carrier in 2024?",
                "Compare fuel litres by aircraft type.",
                "Show monthly fuel cost trend.",
            ],
        },
        {
            "triggers": ["delay", "on time", "on-time", "cancel"],
            "suggestions": [
                "What is the on-time rate by carrier?",
                "Compare average arrival delay by origin airport.",
                "Which service model has the highest cancellation rate?",
            ],
        },
    ],
    "default_recovery_ideas": [
        "What is the on-time rate by carrier?",
        "Compare load factor by service model.",
        "Show monthly passenger revenue trend.",
    ],
}

# Few-shot examples: every one executes against the warehouse
# (tests/unit/test_few_shot_examples.py runs them), and every one demonstrates a
# join because the descriptive attributes are all in dimensions.
few_shot_examples = [
    {
        "question": "What is the on-time rate for each carrier in 2024?",
        "sql": (
            "SELECT c.carrier_name,\n"
            "       ROUND(SUM(CASE WHEN f.arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0\n"
            "             / COUNT(f.arrival_delay_minutes), 4) AS on_time_rate\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_carrier AS c ON f.carrier_code = c.carrier_code\n"
            "WHERE f.cancelled_flag = 0\n"
            "  AND CAST(f.flight_date AS DATE) BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'\n"
            "GROUP BY c.carrier_name\n"
            "ORDER BY on_time_rate DESC"
        ),
    },
    {
        "question": "Compare load factor across service models.",
        "sql": (
            "SELECT c.service_model,\n"
            "       ROUND(SUM(f.passengers) * 1.0 / SUM(f.seats), 4) AS load_factor\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_carrier AS c ON f.carrier_code = c.carrier_code\n"
            "WHERE f.cancelled_flag = 0\n"
            "GROUP BY c.service_model\n"
            "ORDER BY load_factor DESC"
        ),
    },
    {
        "question": "Which origin region has the worst average arrival delay?",
        "sql": (
            "SELECT a.region,\n"
            "       ROUND(AVG(f.arrival_delay_minutes), 2) AS avg_arrival_delay\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_airport AS a ON f.origin_airport = a.airport_code\n"
            "WHERE f.cancelled_flag = 0\n"
            "GROUP BY a.region\n"
            "ORDER BY avg_arrival_delay DESC"
        ),
    },
    {
        "question": "Show monthly passenger revenue.",
        "sql": (
            "SELECT DATE_TRUNC('month', CAST(f.flight_date AS DATE)) AS month_start,\n"
            "       ROUND(SUM(f.passengers * f.fare_eur), 2) AS passenger_revenue\n"
            "FROM fact_flights AS f\n"
            "WHERE f.cancelled_flag = 0\n"
            "GROUP BY DATE_TRUNC('month', CAST(f.flight_date AS DATE))\n"
            "ORDER BY month_start"
        ),
    },
    {
        "question": "What is the cancellation rate by hub size of the origin airport?",
        "sql": (
            "SELECT a.hub_size,\n"
            "       ROUND(SUM(f.cancelled_flag) * 1.0 / COUNT(*), 4) AS cancellation_rate\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_airport AS a ON f.origin_airport = a.airport_code\n"
            "GROUP BY a.hub_size\n"
            "ORDER BY cancellation_rate DESC"
        ),
    },
    {
        "question": "Compare on-time rate month over month for 2024.",
        "sql": (
            "WITH monthly AS (\n"
            "    SELECT DATE_TRUNC('month', CAST(f.flight_date AS DATE)) AS month_start,\n"
            "           SUM(CASE WHEN f.arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0\n"
            "               / COUNT(f.arrival_delay_minutes) AS on_time_rate\n"
            "    FROM fact_flights AS f\n"
            "    WHERE f.cancelled_flag = 0\n"
            "      AND CAST(f.flight_date AS DATE) BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'\n"
            "    GROUP BY DATE_TRUNC('month', CAST(f.flight_date AS DATE))\n"
            ")\n"
            "SELECT month_start,\n"
            "       ROUND(on_time_rate, 4) AS on_time_rate,\n"
            "       ROUND(on_time_rate - LAG(on_time_rate) OVER (ORDER BY month_start), 4) AS delta\n"
            "FROM monthly\n"
            "ORDER BY month_start"
        ),
    },
    {
        "question": "What is fuel cost per seat-kilometre by aircraft type?",
        "sql": (
            "WITH flown AS (\n"
            "    SELECT f.tail_number,\n"
            "           SUM(f.seats * f.distance_km) AS seat_km\n"
            "    FROM fact_flights AS f\n"
            "    WHERE f.cancelled_flag = 0\n"
            "    GROUP BY f.tail_number\n"
            "), burned AS (\n"
            "    SELECT u.tail_number,\n"
            "           SUM(u.fuel_cost_eur) AS fuel_cost\n"
            "    FROM fact_fuel AS u\n"
            "    GROUP BY u.tail_number\n"
            ")\n"
            "SELECT ac.aircraft_type,\n"
            "       ROUND(SUM(burned.fuel_cost) / NULLIF(SUM(flown.seat_km), 0), 6) AS fuel_cost_per_seat_km\n"
            "FROM flown\n"
            "JOIN burned ON flown.tail_number = burned.tail_number\n"
            "JOIN dim_aircraft AS ac ON flown.tail_number = ac.tail_number\n"
            "                       AND ac.valid_to = DATE '9999-12-31'\n"
            "GROUP BY ac.aircraft_type\n"
            "ORDER BY fuel_cost_per_seat_km"
        ),
    },
    {
        "question": "Which routes carried the most passengers in 2024?",
        "sql": (
            "SELECT o.city AS origin_city,\n"
            "       d.city AS destination_city,\n"
            "       SUM(f.passengers) AS passengers\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_airport AS o ON f.origin_airport = o.airport_code\n"
            "JOIN dim_airport AS d ON f.destination_airport = d.airport_code\n"
            "WHERE f.cancelled_flag = 0\n"
            "  AND CAST(f.flight_date AS DATE) BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'\n"
            "GROUP BY o.city, d.city\n"
            "ORDER BY passengers DESC\n"
            "LIMIT 10"
        ),
    },
    {
        "question": "Is the on-time rate worse on public holidays?",
        "sql": (
            "SELECT dd.is_holiday,\n"
            "       ROUND(SUM(CASE WHEN f.arrival_delay_minutes <= 15 THEN 1 ELSE 0 END) * 1.0\n"
            "             / COUNT(f.arrival_delay_minutes), 4) AS on_time_rate\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_date AS dd ON f.flight_date = dd.flight_date\n"
            "WHERE f.cancelled_flag = 0\n"
            "GROUP BY dd.is_holiday\n"
            "ORDER BY dd.is_holiday"
        ),
    },
    {
        "question": "Compare average arrival delay by alliance and season.",
        "sql": (
            "SELECT c.alliance,\n"
            "       dd.season,\n"
            "       ROUND(AVG(f.arrival_delay_minutes), 2) AS avg_arrival_delay\n"
            "FROM fact_flights AS f\n"
            "JOIN dim_carrier AS c ON f.carrier_code = c.carrier_code\n"
            "JOIN dim_date AS dd ON f.flight_date = dd.flight_date\n"
            "WHERE f.cancelled_flag = 0\n"
            "GROUP BY c.alliance, dd.season\n"
            "ORDER BY c.alliance, avg_arrival_delay DESC"
        ),
    },
]

example_questions = [
    {"label": "Carrier punctuality", "question": "What is the on-time rate for each carrier in 2024?"},
    {"label": "Load factor by service model", "question": "Compare load factor across service models"},
    {"label": "Worst delays by region", "question": "Which origin region has the worst average arrival delay?"},
    {"label": "Monthly revenue trend", "question": "Show monthly passenger revenue"},
    {"label": "Holiday effect", "question": "Is the on-time rate worse on public holidays?"},
    {"label": "Fuel efficiency", "question": "What is fuel cost per seat-kilometre by aircraft type?"},
]

payload = {
    "tables": tables,
    "relationships": relationships,
    "business_metrics": business_metrics,
    "dimension_values": dimension_values,
    "dimension_aliases": dimension_aliases,
    "identifier_patterns": identifier_patterns,
    "language": language,
    "few_shot_examples": few_shot_examples,
    "example_questions": example_questions,
    "governance": governance,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')
print("wrote", OUT, len(tables), "tables,", len(business_metrics), "metrics")
