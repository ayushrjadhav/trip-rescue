"""Simulate replacement flights and find options that meet a trip's constraints."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import product
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Airport:
    city: str
    zone: ZoneInfo


AIRPORTS = {
    "ORD": Airport("Chicago", ZoneInfo("America/Chicago")),
    "LGA": Airport("New York", ZoneInfo("America/New_York")),
    "ATL": Airport("Atlanta", ZoneInfo("America/New_York")),
    "DEN": Airport("Denver", ZoneInfo("America/Denver")),
    "SFO": Airport("San Francisco", ZoneInfo("America/Los_Angeles")),
}
PRIMARY_AIRPORTS = tuple(AIRPORTS)
AIRPORTS.update({
    "MDW": Airport("Chicago Midway", AIRPORTS["ORD"].zone),
    "EWR": Airport("Newark", AIRPORTS["LGA"].zone),
    "OAK": Airport("Oakland", AIRPORTS["SFO"].zone),
})

# Alternate airport, estimated transfer minutes, and estimated transfer cost in USD.
NEARBY = {"ORD": ("MDW", 45, 30), "LGA": ("EWR", 65, 40), "SFO": ("OAK", 50, 25)}
PRIMARY_FOR = {alternate: primary for primary, (alternate, _, _) in NEARBY.items()}
ALTERNATE_SAVINGS = {"MDW": 55, "EWR": 50, "OAK": 40}

# Simulated nonstop block times in minutes, shared by both directions.
BLOCK_MINUTES = {
    frozenset((origin, destination)): minutes
    for origin, destination, minutes in (
        ("ORD", "LGA", 135), ("ORD", "ATL", 120), ("ORD", "DEN", 160),
        ("ORD", "SFO", 275), ("LGA", "ATL", 155), ("LGA", "DEN", 255),
        ("LGA", "SFO", 365), ("ATL", "DEN", 200), ("ATL", "SFO", 305),
        ("DEN", "SFO", 165),
    )
}


@dataclass(frozen=True)
class Trip:
    origin: str
    destination: str
    ready_at: datetime
    arrive_by: datetime
    budget: int
    checked_bag: bool


@dataclass(frozen=True)
class GroundTransfer:
    origin: str
    destination: str
    minutes: int
    cost: int


@dataclass(frozen=True)
class LiveFare:
    checked_at: datetime
    search_url: str
    booking_token: str | None
    flight_numbers: tuple[str, ...]
    bag_price_unknown: bool


@dataclass(frozen=True)
class RecoveryOption:
    name: str
    route: tuple[str, ...]
    departure: datetime
    arrival: datetime
    fare: int
    taxes: int
    baggage: int
    before_flight: GroundTransfer | None = None
    after_flight: GroundTransfer | None = None
    live_fare: LiveFare | None = None

    @property
    def price_complete(self) -> bool:
        return self.live_fare is None or not self.live_fare.bag_price_unknown

    @property
    def ground_cost(self) -> int:
        return sum(leg.cost for leg in (self.before_flight, self.after_flight) if leg)

    @property
    def final_arrival(self) -> datetime:
        minutes = self.after_flight.minutes if self.after_flight else 0
        return self.arrival + timedelta(minutes=minutes)

    @property
    def total(self) -> int:
        return self.fare + self.taxes + self.baggage + self.ground_cost


@dataclass(frozen=True)
class SearchResult:
    options: list[RecoveryOption]
    matches: list[RecoveryOption]
    routes: list[tuple[str, str]]
    reason: str
    decisions: tuple[str, ...] = ()
    planner_error: bool = False


def block_minutes(origin: str, destination: str) -> int:
    return BLOCK_MINUTES[frozenset((
        PRIMARY_FOR.get(origin, origin), PRIMARY_FOR.get(destination, destination)
    ))]


def local_time_to_utc(value: datetime, zone: ZoneInfo) -> datetime:
    """Resolve a wall-clock time, rejecting skipped or repeated DST times."""
    candidates = set()
    for fold in (0, 1):
        instant = value.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(instant)
    if len(candidates) != 1:
        raise ValueError("Choose a time outside the local daylight-saving transition.")
    return candidates.pop()


def validation_errors(trip: Trip) -> list[str]:
    errors = []
    if trip.origin == trip.destination:
        errors.append("Choose different departure and destination airports.")
    if trip.budget <= 0:
        errors.append("Enter a budget greater than $0.")
    if trip.arrive_by <= trip.ready_at:
        errors.append("The arrival deadline must be after your earliest departure.")
    return errors


def simulate_options(trip: Trip) -> list[RecoveryOption]:
    """Generate a small, repeatable set of flights for a validated trip."""
    direct = block_minutes(trip.origin, trip.destination)
    connecting_times = {
        hub: block_minutes(trip.origin, hub) + 60 + block_minutes(hub, trip.destination)
        for hub in PRIMARY_AIRPORTS
        if hub not in (
            PRIMARY_FOR.get(trip.origin, trip.origin),
            PRIMARY_FOR.get(trip.destination, trip.destination),
        )
    }
    hub = min(connecting_times, key=connecting_times.__getitem__)
    connection = connecting_times[hub]
    direct_route = (trip.origin, trip.destination)
    connecting_route = (trip.origin, hub, trip.destination)
    scenarios = (
        ("Early nonstop", direct_route, 75, direct, 245, 36, 40),
        ("One-stop connection", connecting_route, 135, connection, 125, 32, 35),
        ("Later nonstop", direct_route, 300, direct, 175, 29, 40),
        ("Next-day nonstop", direct_route, 24 * 60, direct, 105, 24, 35),
    )
    route_surcharge = direct // 5
    savings = ALTERNATE_SAVINGS.get(trip.origin, 0) + ALTERNATE_SAVINGS.get(trip.destination, 0)
    options = []
    for name, route, wait, duration, base_fare, taxes, bag_fee in scenarios:
        departure = trip.ready_at + timedelta(minutes=wait)
        options.append(
            RecoveryOption(
                name, route, departure, departure + timedelta(minutes=duration),
                base_fare + route_surcharge - savings, taxes, bag_fee if trip.checked_bag else 0,
            )
        )
    return options


def matching_options(trip: Trip, options: list[RecoveryOption]) -> list[RecoveryOption]:
    eligible = (
        option for option in options
        if option.price_complete and option.total <= trip.budget
        and option.final_arrival <= trip.arrive_by
    )
    return sorted(eligible, key=lambda option: (option.total, option.final_arrival))


@dataclass(frozen=True)
class SearchRoute:
    origin: str
    destination: str
    before: GroundTransfer | None = None
    after: GroundTransfer | None = None

    @property
    def code(self) -> str:
        return f"{self.origin}-{self.destination}"


def find_nearby_airports(trip: Trip) -> dict[str, SearchRoute]:
    outbound = [None]
    inbound = [None]
    if trip.origin in NEARBY:
        alternate, minutes, cost = NEARBY[trip.origin]
        outbound.append(GroundTransfer(trip.origin, alternate, minutes, cost))
    if trip.destination in NEARBY:
        alternate, minutes, cost = NEARBY[trip.destination]
        inbound.append(GroundTransfer(alternate, trip.destination, minutes, cost))

    routes = {}
    for before, after in product(outbound, inbound):
        if before or after:
            route = SearchRoute(
                before.destination if before else trip.origin,
                after.origin if after else trip.destination,
                before,
                after,
            )
            routes[route.code] = route
    return routes


def search_flights(trip: Trip, route: SearchRoute) -> list[RecoveryOption]:
    transfer_minutes = route.before.minutes if route.before else 0
    flight_trip = replace(
        trip, origin=route.origin, destination=route.destination,
        ready_at=trip.ready_at + timedelta(minutes=transfer_minutes),
    )
    return [
        replace(option, before_flight=route.before, after_flight=route.after)
        for option in simulate_options(flight_trip)
    ]


def search_recovery(
    trip: Trip,
    *,
    include_nearby: bool = False,
    route_search: Callable[[Trip, SearchRoute], list[RecoveryOption]] = search_flights,
    prior_routes: tuple[tuple[str, str], ...] = (),
    live: bool = False,
) -> SearchResult:
    """Expand the search according to the binding constraint, up to four routes."""
    options = list(route_search(trip, SearchRoute(trip.origin, trip.destination)))
    routes = [(trip.origin, trip.destination)]
    matches = matching_options(trip, options)
    if matches and not include_nearby:
        return SearchResult(options, matches, routes, "Original airports meet both limits.")

    in_budget = any(option.price_complete and option.total <= trip.budget for option in options)
    on_time = any(option.final_arrival <= trip.arrive_by for option in options)
    prioritize_time = in_budget or not on_time
    if include_nearby:
        reason = "Checked the original route and every supported nearby route."
    elif prioritize_time:
        reason = "Checked shorter ground transfers first to protect your arrival deadline."
    else:
        reason = (
            "Checked routes with lower ground-transfer costs first to protect your budget."
            if live else "Checked routes with the largest estimated savings first to protect your budget."
        )

    candidates = find_nearby_airports(trip)

    def priority(pair: tuple[GroundTransfer | None, GroundTransfer | None]) -> tuple[int, int]:
        before, after = pair
        minutes = sum(leg.minutes for leg in pair if leg)
        savings = (
            ((0 if live else ALTERNATE_SAVINGS[before.destination]) - before.cost if before else 0)
            + ((0 if live else ALTERNATE_SAVINGS[after.origin]) - after.cost if after else 0)
        )
        return (minutes, -savings) if prioritize_time else (-savings, minutes)

    previously_searched = [
        candidates[f"{origin}-{destination}"] for origin, destination in prior_routes[1:]
    ]
    for route in previously_searched:
        options.extend(route_search(trip, route))
        routes.append((route.origin, route.destination))
    remaining = [route for route in candidates.values() if route not in previously_searched]
    for route in sorted(remaining, key=lambda route: priority((route.before, route.after))):
        options.extend(route_search(trip, route))
        routes.append((route.origin, route.destination))
        matches = matching_options(trip, options)
        if matches and not include_nearby:
            break

    if not candidates:
        reason = "TripRescue has no configured nearby airports for this route."
    return SearchResult(options, matches, routes, reason)


def fallback_options(trip: Trip, options: list[RecoveryOption]) -> list[RecoveryOption]:
    options = [option for option in options if option.price_complete]
    if not options:
        return []
    cheapest_on_time = min(
        (option for option in options if option.final_arrival <= trip.arrive_by),
        key=lambda option: (option.total, option.final_arrival),
        default=None,
    )
    earliest_in_budget = min(
        (option for option in options if option.total <= trip.budget),
        key=lambda option: (option.final_arrival, option.total),
        default=None,
    )
    alternatives = [option for option in (cheapest_on_time, earliest_in_budget) if option]
    if alternatives:
        return alternatives
    # Each extreme is a useful choice when both limits need to change.
    cheapest = min(options, key=lambda option: (option.total, option.final_arrival))
    earliest = min(options, key=lambda option: (option.final_arrival, option.total))
    return [cheapest] if cheapest == earliest else [cheapest, earliest]
