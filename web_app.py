"""FastAPI entry point for TripRescue's standalone web application."""

import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from openai import OpenAIError

from airport_catalog import load_airports

from planner import run_model_planner
from recovery import (
    AIRPORTS, PRIMARY_AIRPORTS, RecoveryOption, SearchResult, SearchRoute, Trip, fallback_options,
    find_nearby_airports, local_time_to_utc, matching_options, search_recovery, validation_errors,
)
from serpapi_flights import FlightProviderError, api_key, next_day_search, search_live
from trip_draft import NEW_YORK_AIRPORTS, DraftEdit, DraftRequest, extract_edit, update_draft

app = FastAPI(title="TripRescue")
LIVE_AIRPORTS = load_airports()
SIM_AIRPORTS = {code: f"{AIRPORTS[code].city} ({code})" for code in PRIMARY_AIRPORTS}


class WebDraftRequest(DraftRequest):
    source: Literal["simulated", "live"] = "simulated"


class RecoveryRequest(BaseModel):
    origin: str
    destination: str
    ready_at: datetime
    arrive_by: datetime
    budget: int = Field(strict=True, gt=0)
    checked_bag: StrictBool = False
    source: Literal["simulated", "live"] = "simulated"
    use_model: StrictBool | None = None
    compare_all: StrictBool = False


def local_instant(value: datetime, airport: str) -> datetime:
    if value.tzinfo is not None:
        raise ValueError("Use the airport's local time without a UTC offset.")
    if value.second or value.microsecond:
        raise ValueError("Use airport times to the nearest minute.")
    return local_time_to_utc(value, AIRPORTS[airport].zone)


def local_iso(value: datetime, airport: str) -> str:
    return value.astimezone(AIRPORTS[airport].zone).isoformat(timespec="minutes")


def itinerary(option: RecoveryOption, trip: Trip) -> dict:
    fare = option.live_fare
    return {
        "name": option.name,
        "route": option.route,
        "departure": local_iso(option.departure, option.route[0]),
        "arrival": local_iso(option.arrival, option.route[-1]),
        "final_arrival": local_iso(option.final_arrival, trip.destination),
        "departure_zone": AIRPORTS[option.route[0]].zone.key,
        "destination_zone": AIRPORTS[trip.destination].zone.key,
        "total": option.total,
        "fare": option.fare,
        "taxes": option.taxes,
        "baggage": option.baggage if option.price_complete else None,
        "transfers": [asdict(leg) for leg in (option.before_flight, option.after_flight) if leg],
        "checked_at": fare.checked_at if fare else None,
        "flight_numbers": fare.flight_numbers if fare else (),
        "search_url": fare.search_url if fare else None,
    }


@app.get("/api/config")
def configuration():
    return {
        "airports": [
            {"code": code, "label": label, "zone": AIRPORTS[code].zone.key}
            for code, label in SIM_AIRPORTS.items()
        ],
        "live_airports": [
            {"code": code, "label": label, "zone": AIRPORTS[code].zone.key}
            for code, label in LIVE_AIRPORTS.items()
        ] if api_key() else [],
        "live_available": bool(api_key()),
        "model_available": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }


@app.post("/api/trip-draft")
def trip_draft(request: WebDraftRequest):
    if not os.getenv("OPENAI_API_KEY", "").strip():
        raise HTTPException(503, "Ask TripRescue is unavailable. You can still fill in your trip below.")
    if not request.message.strip():
        raise HTTPException(422, "Describe what changed about your trip.")
    supported = LIVE_AIRPORTS if request.source == "live" and api_key() else SIM_AIRPORTS
    now = datetime.now(timezone.utc)
    live_clock = now if supported is LIVE_AIRPORTS else None
    if request.use_suggested_departure:
        empty = DraftEdit(origin=None, destination=None, ready_at=None, arrive_by=None,
                          budget=None, checked_bag=None, clarification=None)
        return update_draft(request, empty, supported, now=live_clock)
    if choice := request.airport_choice:
        field = "origin" if request.draft.origin == "NYC" else "destination"
        specific = choice in NEW_YORK_AIRPORTS and choice in supported
        flexible = choice == "NYC" and field == "destination" and supported is LIVE_AIRPORTS
        if getattr(request.draft, field) != "NYC" or not (specific or flexible):
            raise HTTPException(422, "Choose an airport from the current options.")
        edit = DraftEdit(
            origin=choice if field == "origin" else None,
            destination=choice if field == "destination" else None,
            ready_at=None, arrive_by=None, budget=None, checked_bag=None,
            clarification=None, nyc_flexible=choice == "NYC",
        )
        return update_draft(request, edit, supported, now=live_clock)
    try:
        edit = extract_edit(request, now)
    except OpenAIError:
        raise HTTPException(502, "The assistant is unavailable. Try again or use the trip form.") from None
    if edit is None:
        raise HTTPException(422, "Could not read that trip request. Try including a route and deadline.")
    return update_draft(request, edit, supported, now=live_clock)


def search_nyc(trip: Trip, now: datetime) -> SearchResult:
    destinations = [code for code in NEW_YORK_AIRPORTS if code != trip.origin and code in LIVE_AIRPORTS]
    searches = [SearchRoute(trip.origin, code) for code in destinations]
    options = []
    for route in searches:
        options.extend(search_live(trip, route, now=now))
    matches = matching_options(trip, options)
    if not matches and (next_day := next_day_search(trip, now)):
        # Follow up on the airport with the lowest known fare, or JFK when there are no fares.
        cheapest = min(options, key=lambda option: option.total, default=None)
        route = replace(next_day, destination=cheapest.route[-1] if cheapest else destinations[0])
        searches.append(route)
        options.extend(search_live(trip, route, now=now))
        matches = matching_options(trip, options)
    return SearchResult(
        options, matches, [(route.origin, route.destination) for route in searches],
        "Compared New York–area airports. The deadline applies at the airport.",
        searched_dates=tuple(route.outbound_date for route in searches),
    )


@app.post("/api/recover")
def recover(request: RecoveryRequest):
    live = request.source == "live"
    supported = LIVE_AIRPORTS if live else SIM_AIRPORTS
    any_nyc = live and request.destination == "NYC"
    if request.origin not in supported or (request.destination not in supported and not any_nyc):
        raise HTTPException(422, "Choose two supported airports.")
    destination = request.destination
    if any_nyc:
        destination = next(code for code in NEW_YORK_AIRPORTS if code != request.origin)
    try:
        ready_at = local_instant(request.ready_at, request.origin)
        arrive_by = local_instant(request.arrive_by, destination)
    except (OverflowError, ValueError) as error:
        raise HTTPException(422, "Choose valid airport-local dates and times.") from error

    trip = Trip(request.origin, destination, ready_at, arrive_by, request.budget, request.checked_bag)
    if errors := validation_errors(trip):
        raise HTTPException(422, errors[0])

    now = datetime.now(timezone.utc)
    if live and not api_key():
        raise HTTPException(503, "Live flights are unavailable. You can still try simulated flights.")
    if live and trip.arrive_by <= now:
        raise HTTPException(422, "Choose a future arrival deadline for live flights.")
    if live and trip.ready_at < now.replace(second=0, microsecond=0):
        raise HTTPException(422, "Choose an earliest departure time that has not already passed.")
    model_available = bool(os.getenv("OPENAI_API_KEY", "").strip())
    use_model = model_available if request.use_model is None else request.use_model
    if use_model and not model_available:
        raise HTTPException(503, "The OpenAI planner is unavailable. Try the standard search.")

    def live_search(current: Trip, route: SearchRoute) -> list[RecoveryOption]:
        return search_live(current, route, now=now)

    try:
        next_day = next_day_search(trip, now) if live else None
        provider = {"route_search": live_search, "live": True, "next_day": next_day} if live else {}
        if any_nyc:
            result = search_nyc(trip, now)
        elif use_model and not request.compare_all:
            result = run_model_planner(trip, **provider)
        else:
            result = search_recovery(trip, include_nearby=request.compare_all, **provider)
    except OverflowError:
        raise HTTPException(422, "Choose dates within the supported calendar range.") from None
    except FlightProviderError as error:
        raise HTTPException(502, str(error)) from None

    fallbacks = []
    if not result.matches:
        for option in fallback_options(trip, result.options):
            adjusted = replace(trip, budget=max(trip.budget, option.total),
                               arrive_by=max(trip.arrive_by, option.final_arrival))
            fallbacks.append({
                "flight": itinerary(option, trip),
                "extra_cost": adjusted.budget - trip.budget,
                "extra_minutes": (adjusted.arrive_by - trip.arrive_by) // timedelta(minutes=1),
                "budget": adjusted.budget,
                "arrive_by": local_iso(adjusted.arrive_by, trip.destination)[:16],
            })

    unverified = sorted(
        (option for option in result.options if not option.price_complete
         and option.total <= trip.budget and option.final_arrival <= trip.arrive_by),
        key=lambda option: (
            option.total, option.final_arrival,
        ),
    )
    return {
        "source": request.source,
        "deadline": trip.arrive_by,
        "matches": [itinerary(option, trip) for option in result.matches],
        "fallbacks": fallbacks,
        "unverified_baggage": [itinerary(option, trip) for option in unverified[:5]],
        "searched": result.routes,
        "searched_dates": result.searched_dates,
        "reason": result.reason,
        "decisions": result.decisions,
        "planner_error": result.planner_error,
        "can_compare_all": not any_nyc and len(result.routes) < min(4, 1 + len(find_nearby_airports(trip)) + bool(next_day)),
    }


app.mount("/", StaticFiles(directory=Path(__file__).with_name("web"), html=True), name="frontend")
