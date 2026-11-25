"""FastAPI entry point for TripRescue's standalone web application."""

import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool

from planner import run_model_planner
from recovery import (
    AIRPORTS, PRIMARY_AIRPORTS, RecoveryOption, SearchRoute, Trip, fallback_options,
    find_nearby_airports, local_time_to_utc, search_recovery, validation_errors,
)
from serpapi_flights import FlightProviderError, api_key, search_live

app = FastAPI(title="TripRescue")


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


def airport_label(code: str) -> str:
    return f"{AIRPORTS[code].city} ({code})"


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
        "airports": [{"code": code, "label": airport_label(code)} for code in PRIMARY_AIRPORTS],
        "live_available": bool(api_key()),
        "model_available": bool(os.getenv("OPENAI_API_KEY", "").strip()),
    }


@app.post("/api/recover")
def recover(request: RecoveryRequest):
    if request.origin not in PRIMARY_AIRPORTS or request.destination not in PRIMARY_AIRPORTS:
        raise HTTPException(422, "Choose two supported airports.")
    try:
        ready_at = local_instant(request.ready_at, request.origin)
        arrive_by = local_instant(request.arrive_by, request.destination)
    except (OverflowError, ValueError) as error:
        raise HTTPException(422, "Choose valid airport-local dates and times.") from error

    trip = Trip(request.origin, request.destination, ready_at, arrive_by, request.budget, request.checked_bag)
    if errors := validation_errors(trip):
        raise HTTPException(422, errors[0])

    now = datetime.now(timezone.utc)
    live = request.source == "live"
    if live and not api_key():
        raise HTTPException(503, "Live flights are unavailable. You can still try simulated flights.")
    if live and trip.arrive_by <= now:
        raise HTTPException(422, "Choose a future arrival deadline for live flights.")
    model_available = bool(os.getenv("OPENAI_API_KEY", "").strip())
    use_model = model_available if request.use_model is None else request.use_model
    if use_model and not model_available:
        raise HTTPException(503, "The OpenAI planner is unavailable. Try the standard search.")

    def live_search(current: Trip, route: SearchRoute) -> list[RecoveryOption]:
        return search_live(current, route, now=now)

    provider = {"route_search": live_search, "live": True} if live else {}
    try:
        if use_model and not request.compare_all:
            result = run_model_planner(trip, **provider)
        else:
            result = search_recovery(trip, include_nearby=request.compare_all, **provider)
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
        (option for option in result.options if not option.price_complete),
        key=lambda option: (
            option.final_arrival > trip.arrive_by, option.total > trip.budget,
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
        "reason": result.reason,
        "decisions": result.decisions,
        "planner_error": result.planner_error,
        "can_compare_all": len(result.routes) < 1 + len(find_nearby_airports(trip)),
    }


app.mount("/", StaticFiles(directory=Path(__file__).with_name("web"), html=True), name="frontend")
