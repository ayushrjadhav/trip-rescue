"""Read-only Google Flights searches through SerpApi."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlsplit

import requests

from recovery import AIRPORTS, LiveFare, RecoveryOption, SearchRoute, Trip, local_time_to_utc

ENDPOINT = "https://serpapi.com/search.json"


class FlightProviderError(Exception):
    """A provider error safe to display without exposing credentials."""


@dataclass(frozen=True)
class Seller:
    name: str
    price: int | None
    baggage: tuple[str, ...]
    url: str | None


def api_key() -> str:
    return os.getenv("SERPAPI_API_KEY", "").strip()


def request_flights(**params) -> dict:
    key = api_key()
    if not key:
        raise FlightProviderError("Set SERPAPI_API_KEY before starting a live search.")
    try:
        response = requests.get(
            ENDPOINT,
            params={
                "engine": "google_flights", "api_key": key, "no_cache": "true",
                "type": 2, "adults": 1, "currency": "USD", "hl": "en", "gl": "us",
                **params,
            },
            timeout=(5, 25),
            allow_redirects=False,
        )
        if response.status_code in (401, 403):
            raise FlightProviderError("SerpApi rejected the key or denied access.")
        if response.status_code == 429:
            raise FlightProviderError("SerpApi's search quota or rate limit was reached.")
        if response.status_code != 200:
            raise FlightProviderError("SerpApi could not complete the request. Try again.")
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise FlightProviderError("Could not read the SerpApi response. Try again.") from None

    if not isinstance(payload, dict):
        raise FlightProviderError("SerpApi returned an unexpected response.")
    error = payload.get("error")
    if error:
        if "hasn't returned any results" in str(error).lower():
            return {}
        raise FlightProviderError("SerpApi could not complete the search. Check your account or try again.")
    metadata = payload.get("search_metadata")
    if metadata is not None and (
        not isinstance(metadata, dict) or metadata.get("status", "Success") != "Success"
    ):
        raise FlightProviderError("SerpApi did not return a completed search. Try again.")
    return payload


def google_travel_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.netloc == "www.google.com" and url.path.startswith("/travel/"):
            return value
    except ValueError:
        pass
    return None


def airport_time(value: str, airport: str) -> datetime:
    if not isinstance(value, str) or len(value) < 16:
        raise ValueError("Missing airport time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return local_time_to_utc(parsed, AIRPORTS[airport].zone)


def parse_offer(
    group: dict, route: SearchRoute, trip: Trip, checked_at: datetime, search_url: str,
) -> RecoveryOption:
    price = group["price"]
    if type(price) is not int or price < 0:
        raise ValueError("Invalid price")
    flights = group["flights"]
    departures = tuple(leg["departure_airport"]["id"] for leg in flights)
    arrivals = tuple(leg["arrival_airport"]["id"] for leg in flights)
    if (
        departures[0] != route.origin or arrivals[-1] != route.destination
        or departures[1:] != arrivals[:-1]
        or any(not re.fullmatch(r"[A-Z]{3}", code) for code in (*departures, arrivals[-1]))
    ):
        raise ValueError("Unexpected airports")
    departure = airport_time(flights[0]["departure_airport"]["time"], route.origin)
    arrival = airport_time(flights[-1]["arrival_airport"]["time"], route.destination)
    if arrival <= departure:
        raise ValueError("Invalid itinerary timing")
    numbers = tuple(leg["flight_number"] for leg in flights if isinstance(leg.get("flight_number"), str))
    token = group.get("booking_token")
    stops = len(flights) - 1
    name = "Nonstop" if not stops else f"{stops} stop" if stops == 1 else f"{stops} stops"
    if group.get("separate_tickets"):
        name += " · Separate tickets"
    return RecoveryOption(
        name, (*departures, arrivals[-1]), departure, arrival, price, 0, 0,
        before_flight=route.before, after_flight=route.after,
        live_fare=LiveFare(
            checked_at, search_url, token if isinstance(token, str) and token else None,
            numbers, trip.checked_bag,
        ),
    )


def search_live(trip: Trip, route: SearchRoute, *, now: datetime | None = None) -> list[RecoveryOption]:
    # Each route consumes at most one search, on its first eligible local departure date.
    now = now or datetime.now(timezone.utc)
    earliest = max(trip.ready_at, now) + timedelta(minutes=route.before.minutes if route.before else 0)
    local = earliest.astimezone(AIRPORTS[route.origin].zone)
    payload = request_flights(
        departure_id=route.origin, arrival_id=route.destination,
        outbound_date=local.date().isoformat(), outbound_times=f"{local.hour},23", show_hidden="true",
    )
    checked_at = datetime.now(timezone.utc)
    metadata = payload.get("search_metadata") or {}
    search_url = google_travel_url(metadata.get("google_flights_url")) or "https://www.google.com/travel/flights"
    options = {}
    parsed_count = group_count = 0
    for key in ("best_flights", "other_flights"):
        groups = payload.get(key, [])
        if not isinstance(groups, list):
            raise FlightProviderError("SerpApi returned an unexpected flight list.")
        for group in groups:
            group_count += 1
            try:
                option = parse_offer(group, route, trip, checked_at, search_url)
            except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                continue
            parsed_count += 1
            if option.departure >= earliest and option.departure >= checked_at:
                identity = (
                    option.route, option.departure, option.arrival,
                    option.total, option.live_fare.flight_numbers,
                )
                options.setdefault(identity, option)
    if group_count and not parsed_count:
        raise FlightProviderError("SerpApi returned flights TripRescue could not validate.")
    return list(options.values())


def lookup_sellers(option: RecoveryOption) -> list[Seller]:
    fare = option.live_fare
    if not fare or not fare.booking_token:
        return []
    origin, destination = option.route[0], option.route[-1]
    day = option.departure.astimezone(AIRPORTS[origin].zone).date()
    payload = request_flights(
        departure_id=origin, arrival_id=destination, outbound_date=day.isoformat(),
        booking_token=fare.booking_token,
    )
    sellers = []
    for group in payload.get("booking_options") or []:
        if not isinstance(group, dict):
            continue
        details = group.get("together") or group.get("departing")
        if not isinstance(details, dict) or not isinstance(details.get("book_with"), str):
            continue
        price = details.get("price")
        booking = details.get("booking_request")
        booking = booking if isinstance(booking, dict) else {}
        url = google_travel_url(booking.get("url")) if not booking.get("post_data") else None
        bags = details.get("baggage_prices")
        bags = bags if isinstance(bags, list) else []
        sellers.append(Seller(
            details["book_with"], price if type(price) is int and price >= 0 else None,
            tuple(bag for bag in bags if isinstance(bag, str)), url,
        ))
    return sellers
