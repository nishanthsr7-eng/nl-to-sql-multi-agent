"""Generate the ``airline`` domain -- a second vertical through the same contract.

Phase 4 task 3 asks for a second domain "loaded through the same
``semantic_layer.json`` contract with zero code changes". That claim is only
worth making if the second warehouse is genuinely unlike the first: different
grain, different join shape, different metric algebra, none of the FMCG
vocabulary. On-time performance qualifies -- its headline metrics are *rates*
over a count of flights rather than sums of money, which is exactly the kind of
thing a semantic layer written for a sales fact would quietly fail to express.

**This data is synthetic, and the repo says so everywhere it is reported.** The
public on-time datasets (BTS, OpenFlights) are hundreds of megabytes and are not
checked into a portfolio repo; generating under a fixed seed keeps the numbers
reproducible, which is the property the eval timeline actually needs. Nothing
here should be read as a measurement of real airline operations.

Two structural traps are built in deliberately, mirroring the ones the retail
star carries:

* **A grain mismatch.** ``fact_flights`` is one row per (flight_date,
  flight_number); ``fact_fuel`` is one row per (fuel_date, tail_number). A tail
  flies several times a day, so joining the two on the tail and the date
  multiplies every flight row by that day's fuel records -- and SUM(passengers)
  silently inflates. That is the validator's grain check, restated in a domain
  it has never seen.
* **A duplicated column name at two grains.** ``seats`` is on the flight (how
  many were sold on that departure) and ``seat_capacity`` is on the aircraft
  (how many the airframe has). They are close enough to be confused and are not
  interchangeable, which is the airline version of ``region`` living on both a
  dimension and the modelling table.

Phase 4 task 4 adds a third category on top: **deliberately dirty data**, with
gold cases asserting correct handling. It lives here rather than in the retail
star for one reason -- retail's totals are a published baseline (revenue is
19,951,300.58 to the cent, and every Phase 3 gold answer is scored against it),
so injecting nulls or duplicate keys there would move numbers the README
reports. This domain has no published figure to protect, so the defects can be
real rather than simulated:

* **Unreported delays.** ``arrival_delay_minutes`` is NULL on a small share of
  operated flights -- the feed did not carry one. This is the nastiest of the
  three, because the obvious on-time formula
  ``SUM(CASE WHEN delay <= 15 THEN 1 ELSE 0 END) / COUNT(*)`` silently scores
  every unknown as *late*: a NULL falls to the ELSE branch, and COUNT(*) still
  counts the row. The correct denominator excludes them.
* **A re-registered tail.** Two airframes changed operator mid-period, so
  ``dim_aircraft`` holds two rows for those tails and its declared grain is
  (tail_number, valid_from) -- a slowly-changing dimension, not a lookup. A join
  on ``tail_number`` alone therefore does not cover the grain, and the
  validator's fan-out check rejects it rather than letting the duplicate quietly
  double that tail's flights.
* **Out-of-range dates.** A handful of flights carry a 2019 date the operational
  feed should never have produced. They are outside the declared period and have
  no ``dim_date`` row, so an inner join drops them and a plain
  ``SELECT SUM(...) FROM fact_flights`` does not -- two "totals" that disagree,
  which is what makes an unbounded aggregate over dirty data worth catching.

None of the three is hidden: all are declared in the semantic layer's table
descriptions, because a warehouse whose defects are undocumented is a different
(and much worse) test than one whose defects the model is told about and still
gets wrong.

Run:  python scripts/build_airline_domain.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "raw" / "airline"

# Same discipline as the retail generator: a fixed seed is the difference
# between "generated data" and "arbitrary data". Re-running must reproduce the
# warehouse any reported number was measured against.
SEED = 20260920

START_DATE = "2023-01-01"
END_DATE = "2024-12-31"

# Airports, with the hub tier that drives their traffic share. The hub/spoke
# split is what makes "on-time rate by hub size" a question with a real answer
# rather than noise.
AIRPORTS: list[tuple[str, str, str, str, str]] = [
    # code, city, country, region, hub_size
    ("LHR", "London", "UK", "Europe-West", "mega_hub"),
    ("CDG", "Paris", "France", "Europe-West", "mega_hub"),
    ("AMS", "Amsterdam", "Netherlands", "Europe-West", "large_hub"),
    ("FRA", "Frankfurt", "Germany", "Europe-Central", "mega_hub"),
    ("MUC", "Munich", "Germany", "Europe-Central", "large_hub"),
    ("WAW", "Warsaw", "Poland", "Europe-Central", "medium_hub"),
    ("MAD", "Madrid", "Spain", "Europe-South", "large_hub"),
    ("FCO", "Rome", "Italy", "Europe-South", "large_hub"),
    ("ATH", "Athens", "Greece", "Europe-South", "medium_hub"),
    ("ARN", "Stockholm", "Sweden", "Europe-North", "medium_hub"),
    ("CPH", "Copenhagen", "Denmark", "Europe-North", "large_hub"),
    ("HEL", "Helsinki", "Finland", "Europe-North", "small_hub"),
]

# Carriers, with the service model that drives their delay and load behaviour.
CARRIERS: list[tuple[str, str, str, str]] = [
    # code, name, alliance, service_model
    ("NV", "Northvale Air", "Skyway", "FullService"),
    ("AL", "Altus Airlines", "Skyway", "FullService"),
    ("BQ", "BlueQuay", "Meridian", "FullService"),
    ("JT", "Jetterra", "Unaligned", "LowCost"),
    ("SW", "Swiftwing", "Unaligned", "LowCost"),
    ("RG", "Regia Connect", "Meridian", "Regional"),
    # "Unaligned" rather than "None": a literal None in a CSV column round-trips
    # through pandas as NaN, which would make the dimension value list contain a
    # float and the planner unable to match the word a user would actually type.
]

AIRCRAFT_TYPES: list[tuple[str, str, int, str]] = [
    # type, manufacturer, seat_capacity, body
    ("A320neo", "Airbus", 180, "Narrowbody"),
    ("A321neo", "Airbus", 220, "Narrowbody"),
    ("B737-800", "Boeing", 189, "Narrowbody"),
    ("B787-9", "Boeing", 290, "Widebody"),
    ("A350-900", "Airbus", 315, "Widebody"),
    ("E190", "Embraer", 100, "Regional"),
]

# How many tails each carrier operates, and which body types it flies. A
# regional carrier with a widebody would make "load factor by service model"
# meaningless.
FLEET: dict[str, tuple[int, tuple[str, ...]]] = {
    "NV": (14, ("Narrowbody", "Widebody")),
    "AL": (12, ("Narrowbody", "Widebody")),
    "BQ": (10, ("Narrowbody",)),
    "JT": (12, ("Narrowbody",)),
    "SW": (10, ("Narrowbody",)),
    "RG": (8, ("Regional",)),
}

# Departures per carrier per day. Kept small enough that the whole warehouse is
# a few megabytes of CSV -- the point of this domain is the contract, not scale.
DEPARTURES_PER_DAY: dict[str, int] = {
    "NV": 10, "AL": 9, "BQ": 7, "JT": 9, "SW": 7, "RG": 6,
}

# Delay behaviour by service model: (median minutes, spread, share of flights
# that push past the 15-minute on-time threshold at all).
DELAY_PROFILE: dict[str, tuple[float, float, float]] = {
    "FullService": (4.0, 18.0, 0.22),
    "LowCost": (7.0, 24.0, 0.30),
    "Regional": (9.0, 27.0, 0.34),
}

# Mega hubs are congested; small ones are not. Added to the departure delay.
HUB_DELAY_MINUTES: dict[str, float] = {
    "mega_hub": 6.0, "large_hub": 3.0, "medium_hub": 1.0, "small_hub": 0.0,
}

SEASON_BY_MONTH: dict[int, str] = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Autumn", 10: "Autumn", 11: "Autumn",
}

# European public holidays that move traffic, as month-day pairs. Not exhaustive
# -- enough for "does the holiday flag change the on-time rate" to be answerable.
HOLIDAYS: set[tuple[int, int]] = {
    (1, 1), (4, 1), (5, 1), (8, 15), (11, 1), (12, 24), (12, 25), (12, 26), (12, 31),
}

# Fuel is recorded per tail per day, not per flight: that is how the operation
# actually meters it, and it is what makes the grain mismatch real rather than
# contrived.
LITRES_PER_SEAT_KM: float = 0.031

# --- Deliberate defects (Phase 4 task 4). Every one is bounded and declared. --

# Share of operated flights whose arrival delay the feed did not report.
NULL_DELAY_SHARE: float = 0.015

# Tails that changed operator mid-period, and the date they moved. Each produces
# two dim_aircraft rows, which is what makes that table a slowly-changing
# dimension rather than a lookup.
REREGISTRATIONS: dict[str, tuple[str, str]] = {
    # tail -> (new carrier, date the change took effect)
    "BQ-104": ("NV", "2024-03-01"),
    "SW-103": ("JT", "2024-07-01"),
}

# Flights the feed stamped with a date years outside the declared period. Kept
# to a handful: the defect has to be findable by a careful query and invisible
# to a careless one, which a large share would not be.
OUT_OF_RANGE_FLIGHTS: int = 7
OUT_OF_RANGE_DATE: str = "2019-06-14"


def build_dim_airport() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "airport_code": code,
                "city": city,
                "country": country,
                "region": region,
                "hub_size": hub,
            }
            for code, city, country, region, hub in AIRPORTS
        ]
    )


def build_dim_carrier() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "carrier_code": code,
                "carrier_name": name,
                "alliance": alliance,
                "service_model": model,
            }
            for code, name, alliance, model in CARRIERS
        ]
    )


def build_dim_aircraft(rng: np.random.Generator) -> pd.DataFrame:
    """One row per (tail_number, valid_from). ``seat_capacity`` is the airframe's.

    Not one row per tail: two airframes changed operator mid-period, so those
    tails carry two rows with disjoint validity windows. That is an ordinary
    slowly-changing dimension and it is declared as one -- which is exactly why
    a join on ``tail_number`` alone is a fan-out and gets rejected, instead of
    silently double-counting those two tails' flights.
    """
    by_body: dict[str, list[tuple[str, str, int, str]]] = {}
    for entry in AIRCRAFT_TYPES:
        by_body.setdefault(entry[3], []).append(entry)

    rows = []
    for carrier, (fleet_size, bodies) in FLEET.items():
        for index in range(fleet_size):
            body = bodies[index % len(bodies)]
            aircraft_type, manufacturer, capacity, _ = by_body[body][
                int(rng.integers(0, len(by_body[body])))
            ]
            tail = f"{carrier}-{index + 101}"
            base = {
                "tail_number": tail,
                "aircraft_type": aircraft_type,
                "manufacturer": manufacturer,
                "body_type": body,
                "seat_capacity": capacity,
                "year_built": int(rng.integers(2008, 2023)),
            }
            if tail in REREGISTRATIONS:
                new_carrier, changed_on = REREGISTRATIONS[tail]
                rows.append(
                    {**base, "carrier_code": carrier,
                     "valid_from": START_DATE, "valid_to": changed_on}
                )
                rows.append(
                    {**base, "carrier_code": new_carrier,
                     "valid_from": changed_on, "valid_to": "9999-12-31"}
                )
            else:
                rows.append(
                    {**base, "carrier_code": carrier,
                     "valid_from": START_DATE, "valid_to": "9999-12-31"}
                )
    # valid_to is exclusive, so the windows tile the period without overlapping:
    # a BETWEEN on them would match the changeover date twice and reintroduce
    # the duplicate this design exists to make explicit.
    return pd.DataFrame(rows)


def build_dim_date(dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "flight_date": dates.strftime("%Y-%m-%d"),
            "day_of_week": dates.day_name(),
            "is_weekend": (dates.dayofweek >= 5).astype(int),
            "fiscal_quarter": [f"{d.year}-Q{(d.month - 1) // 3 + 1}" for d in dates],
            "season": [SEASON_BY_MONTH[d.month] for d in dates],
            "is_holiday": [int((d.month, d.day) in HOLIDAYS) for d in dates],
        }
    )


def _route_distance(rng: np.random.Generator, origin: str, destination: str) -> int:
    """A stable pseudo-distance per unordered route.

    Derived from the route rather than drawn per flight, because "average
    distance by carrier" is meaningless if the same route is 400 km on Monday
    and 2,000 km on Tuesday.
    """
    key = "".join(sorted((origin, destination)))
    seed = sum(ord(ch) for ch in key)
    local = np.random.default_rng(SEED + seed)
    return int(local.integers(350, 2600))


def build_facts(
    rng: np.random.Generator, dates: pd.DatetimeIndex, aircraft: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The flights fact and the fuel fact, at two deliberately different grains."""
    airports = build_dim_airport().set_index("airport_code")
    tails_by_carrier = {
        carrier: group["tail_number"].tolist()
        for carrier, group in aircraft.groupby("carrier_code")
    }
    capacity_by_tail = dict(zip(aircraft["tail_number"], aircraft["seat_capacity"], strict=True))
    codes = [code for code, *_ in AIRPORTS]

    flight_rows: list[dict] = []
    # (date, tail) -> seat-km flown, accumulated so the fuel fact is consistent
    # with the flying that produced it rather than an independent random series.
    seat_km: dict[tuple[str, str], float] = {}

    for date in dates:
        date_str = date.strftime("%Y-%m-%d")
        season_factor = 1.12 if SEASON_BY_MONTH[date.month] == "Summer" else 1.0
        holiday = (date.month, date.day) in HOLIDAYS

        for carrier, _, _, model in CARRIERS:
            median, spread, tail_share = DELAY_PROFILE[model]
            tails = tails_by_carrier[carrier]

            for departure in range(DEPARTURES_PER_DAY[carrier]):
                origin = codes[int(rng.integers(0, len(codes)))]
                destination = origin
                while destination == origin:
                    destination = codes[int(rng.integers(0, len(codes)))]

                tail = tails[int(rng.integers(0, len(tails)))]
                capacity = capacity_by_tail[tail]
                distance = _route_distance(rng, origin, destination)

                # Cancellations are rare, weather-ish, and worse on holidays.
                cancelled = int(rng.random() < (0.018 if holiday else 0.009))

                congestion = HUB_DELAY_MINUTES[str(airports.at[origin, "hub_size"])]
                base = rng.normal(median + congestion, spread * 0.4)
                if rng.random() < tail_share:
                    # The long tail: a real delay distribution is not Gaussian,
                    # and an on-time *rate* computed over a Gaussian is a
                    # different statistic from the one airlines report.
                    base += rng.exponential(35.0)
                if holiday:
                    base += rng.normal(8.0, 6.0)
                departure_delay = 0.0 if cancelled else max(-12.0, base)
                # Time is made up in the air, but not much and not reliably.
                arrival_delay = (
                    0.0 if cancelled else max(-20.0, departure_delay - rng.normal(3.0, 7.0))
                )

                # ``seats`` is the seats *offered* on that departure, which is the
                # airframe's capacity minus whatever was blocked off; passengers
                # is who actually boarded. Load factor is the ratio, so the two
                # must not be derived from each other or the metric measures the
                # generator instead of the operation.
                load = float(np.clip(rng.normal(0.80 * season_factor, 0.10), 0.35, 1.0))
                seats_offered = 0 if cancelled else capacity - int(rng.integers(0, 6))
                passengers = 0 if cancelled else min(seats_offered, int(round(capacity * load)))
                seats_sold = seats_offered
                fare = float(
                    np.round(
                        (38 + distance * 0.061) * (0.72 if model == "LowCost" else 1.0)
                        * rng.uniform(0.85, 1.25),
                        2,
                    )
                )

                if not cancelled:
                    seat_km[(date_str, tail)] = (
                        seat_km.get((date_str, tail), 0.0) + capacity * distance
                    )

                flight_rows.append(
                    {
                        "flight_date": date_str,
                        "flight_number": f"{carrier}{1000 + departure * 7 + int(rng.integers(0, 7))}",
                        "carrier_code": carrier,
                        "tail_number": tail,
                        "origin_airport": origin,
                        "destination_airport": destination,
                        "distance_km": distance,
                        "seats": seats_sold,
                        "passengers": passengers,
                        "fare_eur": fare,
                        "departure_delay_minutes": int(round(departure_delay)),
                        "arrival_delay_minutes": int(round(arrival_delay)),
                        "cancelled_flag": cancelled,
                    }
                )

    flights = pd.DataFrame(flight_rows)

    # Unreported delays. Applied to operated flights only -- a cancelled flight
    # has no arrival at all, which is a different kind of missing.
    operated = flights.index[flights["cancelled_flag"] == 0]
    unreported = rng.choice(
        operated, size=int(len(operated) * NULL_DELAY_SHARE), replace=False
    )
    flights.loc[unreported, "arrival_delay_minutes"] = np.nan

    # Feed rows stamped years outside the declared period. Taken from existing
    # flights so every other column stays internally consistent: the date is the
    # defect, and a row that was also wrong about its carrier or its route would
    # be testing several things at once.
    stray = flights.sample(n=OUT_OF_RANGE_FLIGHTS, random_state=SEED).copy()
    stray["flight_date"] = OUT_OF_RANGE_DATE
    stray["flight_number"] = [f"XX{9000 + i}" for i in range(len(stray))]
    flights = pd.concat([flights, stray], ignore_index=True)
    # A flight number can repeat within a day across the random draw above; the
    # declared grain is (flight_date, flight_number), so it has to actually hold
    # or the validator's grain check would be policing a lie.
    flights = flights.drop_duplicates(subset=["flight_date", "flight_number"], keep="first")

    fuel_rows = [
        {
            "fuel_date": date_str,
            "tail_number": tail,
            "fuel_litres": int(round(km * LITRES_PER_SEAT_KM)),
            "fuel_cost_eur": float(np.round(km * LITRES_PER_SEAT_KM * 0.92, 2)),
        }
        for (date_str, tail), km in sorted(seat_km.items())
    ]
    return flights, pd.DataFrame(fuel_rows)


# Crew given/family name pools. Deliberately invented names over a fixed seed:
# the point of this table is to carry columns the semantic layer can tag as
# personal data so the masking path has something real to mask, and inventing
# the values is what keeps a synthetic warehouse synthetic.
CREW_GIVEN = (
    "Alina", "Bartosz", "Cecilia", "Dmitri", "Elena", "Fabian", "Greta", "Henrik",
    "Ilona", "Jonas", "Katarina", "Lukas", "Marta", "Nikolai", "Olga", "Piotr",
)
CREW_FAMILY = (
    "Adamczyk", "Berg", "Cordier", "Duarte", "Eriksson", "Falk", "Grabowski",
    "Halvorsen", "Ivarsson", "Janssen", "Kaminski", "Lindqvist", "Moreau",
    "Nowicki", "Olsen", "Petrov",
)
CREW_ROLES = ("Captain", "First Officer", "Senior Cabin Crew", "Cabin Crew")
CREW_PER_CARRIER = 12


def build_dim_crew(rng: np.random.Generator, airports: pd.DataFrame) -> pd.DataFrame:
    """One row per crew member, carrying the domain's only personal data.

    It joins to dim_carrier and to nothing else. That is deliberate: the PII
    masking path needs a tagged column that a question can actually select, and
    hanging it off the flight fact would have changed fact_flights' schema --
    and therefore every airline eval number -- to demonstrate something that has
    nothing to do with flights.
    """
    bases = list(airports["airport_code"])
    rows = []
    for carrier_index, (code, _name, _alliance, _model) in enumerate(CARRIERS):
        for seat in range(CREW_PER_CARRIER):
            given = CREW_GIVEN[int(rng.integers(len(CREW_GIVEN)))]
            family = CREW_FAMILY[int(rng.integers(len(CREW_FAMILY)))]
            crew_id = f"{code}-CR-{seat + 1:03d}"
            rows.append(
                {
                    "crew_id": crew_id,
                    "carrier_code": code,
                    "crew_name": f"{given} {family}",
                    # The address is derived from the id, not from the name, so
                    # redacting the local part genuinely removes the identity
                    # rather than leaving it legible in the domain.
                    "crew_email": f"{crew_id.lower()}@{code.lower()}-crew.example",
                    "crew_phone": f"+48{rng.integers(100000000, 999999999)}",
                    "crew_role": CREW_ROLES[int(rng.integers(len(CREW_ROLES)))],
                    "base_airport": bases[(carrier_index * 3 + seat) % len(bases)],
                    "hire_year": int(rng.integers(2005, 2024)),
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    rng = np.random.default_rng(SEED)
    dates = pd.date_range(START_DATE, END_DATE, freq="D")

    aircraft = build_dim_aircraft(rng)
    flights, fuel = build_facts(rng, dates, aircraft)
    airports = build_dim_airport()
    crew = build_dim_crew(rng, airports)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tables = {
        "fact_flights": flights,
        "fact_fuel": fuel,
        "dim_airport": airports,
        "dim_carrier": build_dim_carrier(),
        "dim_aircraft": aircraft,
        "dim_date": build_dim_date(dates),
        "dim_crew": crew,
    }
    for name, frame in tables.items():
        frame.to_csv(OUT_DIR / f"{name}.csv", index=False)
        print(f"  {name}: {len(frame):,} rows")

    # The declared grain is load-bearing -- the validator's fan-out check trusts
    # it -- so it is asserted here rather than hoped for, exactly as the retail
    # generator hard-fails on a broken store allocation.
    assert not flights.duplicated(subset=["flight_date", "flight_number"]).any(), (
        "fact_flights is not unique on its declared grain (flight_date, flight_number)."
    )
    # dim_aircraft's grain includes valid_from precisely because tail_number
    # alone is no longer unique. Both halves are asserted: that the declared
    # grain holds, and that the defect the gold cases depend on is present.
    assert not aircraft.duplicated(subset=["tail_number", "valid_from"]).any(), (
        "dim_aircraft is not unique on its declared grain (tail_number, valid_from)."
    )
    assert aircraft["tail_number"].duplicated().any(), (
        "the re-registered tails are gone -- the duplicate-key gold cases measure nothing."
    )
    assert flights["arrival_delay_minutes"].isna().any(), (
        "no unreported delays -- the null-handling gold case measures nothing."
    )
    assert not crew.duplicated(subset=["crew_id"]).any(), (
        "dim_crew is not unique on its declared grain (crew_id)."
    )
    assert not fuel.duplicated(subset=["fuel_date", "tail_number"]).any(), (
        "fact_fuel is not unique on its declared grain (fuel_date, tail_number)."
    )

    flown = flights[flights["cancelled_flag"] == 0]
    reported = flown[flown["arrival_delay_minutes"].notna()]
    on_time = (reported["arrival_delay_minutes"] <= 15).mean()
    print(
        f"\nwritten to {OUT_DIR}\n"
        f"  flights flown: {len(flown):,}  on-time (<=15 min): {on_time:.1%} "
        f"of the {len(reported):,} with a reported delay\n"
        f"  load factor:   {flown['passengers'].sum() / flown['seats'].sum():.1%}\n"
        f"  declared defects: {len(flown) - len(reported):,} unreported delays, "
        f"{len(REREGISTRATIONS)} re-registered tails, "
        f"{OUT_OF_RANGE_FLIGHTS} out-of-range dates"
    )


if __name__ == "__main__":
    main()
