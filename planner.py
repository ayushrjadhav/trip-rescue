"""Bounded OpenAI tool calling over TripRescue's simulated inventory."""

import json
import os
from dataclasses import replace

from recovery import (
    ALTERNATE_SAVINGS, RecoveryOption, SearchResult, SearchRoute, Trip,
    find_nearby_airports, matching_options, search_flights, search_recovery,
)

DEFAULT_MODEL = "gpt-4.1-mini"

INSTRUCTIONS = (
    "You are TripRescue's route planner. The original route was checked first. "
    "Use the tools to find a qualifying itinerary in as few searches as possible. "
    "Use the observed costs and final arrival times to decide what to search next. "
    "Account for both airport transfers. Estimates are hints, not availability. "
    "Keep the traveler's budget and deadline fixed. Give one short concrete reason "
    "for each route choice. Python independently validates all results."
)


class PlannerError(Exception):
    """The model did not return a valid next tool call."""


def make_client():
    from openai import OpenAI

    return OpenAI(timeout=15, max_retries=0)


def tool(name: str, description: str, properties: dict) -> dict:
    return {
        "type": "function", "name": name, "description": description, "strict": True,
        "parameters": {
            "type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False,
        },
    }


def observe(options: list[RecoveryOption]) -> list[dict]:
    return [
        {"flight": option.name, "known_cost_usd": option.total,
         "price_complete": option.price_complete,
         "final_arrival_utc": option.final_arrival.isoformat()}
        for option in options
    ]


def call_tool(client, model: str, messages: list, definition: dict):
    response = client.responses.create(
        model=model, instructions=INSTRUCTIONS, input=messages, store=False,
        tools=[definition], tool_choice={"type": "function", "name": definition["name"]},
        parallel_tool_calls=False, max_output_tokens=256,
    )
    calls = [item for item in response.output if item.type == "function_call"]
    if len(calls) != 1 or calls[0].name != definition["name"]:
        raise PlannerError("Expected one call to the requested tool.")
    try:
        arguments = json.loads(calls[0].arguments)
    except (TypeError, ValueError) as error:
        raise PlannerError("Invalid tool arguments.") from error
    if not isinstance(arguments, dict) or set(arguments) != set(definition["parameters"]["required"]):
        raise PlannerError("Unexpected tool arguments.")
    messages.extend(response.output)
    return calls[0].call_id, arguments


def record_output(messages: list, call_id: str, result) -> None:
    messages.append({
        "type": "function_call_output", "call_id": call_id, "output": json.dumps(result),
    })


def estimate(route: SearchRoute, *, live: bool) -> dict:
    details = {
        "route": route.code,
        "outbound_date": route.outbound_date.isoformat() if route.outbound_date else None,
        "departure_transfer_minutes": route.before.minutes if route.before else 0,
        "arrival_transfer_minutes": route.after.minutes if route.after else 0,
        "transfer_cost_usd": sum(leg.cost for leg in (route.before, route.after) if leg),
    }
    if not live:
        details["estimated_net_savings_usd"] = (
            ALTERNATE_SAVINGS.get(route.origin, 0) + ALTERNATE_SAVINGS.get(route.destination, 0)
            - details["transfer_cost_usd"]
        )
    return details


def search_with_model(
    trip: Trip, client, *, model: str = DEFAULT_MODEL, route_search=search_flights, live=False,
    next_day: SearchRoute | None = None,
) -> SearchResult:
    from openai import OpenAIError

    original = SearchRoute(trip.origin, trip.destination)
    inventory = {original.code: route_search(trip, original)}

    def cached_search(current_trip: Trip, route: SearchRoute) -> list[RecoveryOption]:
        if route.code not in inventory:
            inventory[route.code] = route_search(current_trip, route)
        return inventory[route.code]

    options = list(inventory[original.code])
    routes = [(original.origin, original.destination)]
    matches = matching_options(trip, options)
    nearby = find_nearby_airports(trip)
    if next_day:
        nearby[next_day.code] = next_day
    if matches or not nearby:
        return search_recovery(trip, route_search=cached_search, live=live)

    messages = [{
        "role": "user", "content": json.dumps({
            "original_route": original.code, "budget_usd": trip.budget,
            "deadline_utc": trip.arrive_by.isoformat(),
            "original_route_results": observe(options),
        }),
    }]
    decisions = []
    followups = []
    try:
        discovery = tool("list_search_options", "List nearby airport and next-day search options.", {})
        call_id, _ = call_tool(client, model, messages, discovery)
        record_output(messages, call_id, [estimate(route, live=live) for route in nearby.values()])
        while nearby and not matches and len(routes) < 4:
            search = tool("search_flights", "Search one untried airport pair and departure date.", {
                "route": {"type": "string", "enum": list(nearby)},
                "reason": {"type": "string"},
            })
            call_id, args = call_tool(client, model, messages, search)
            code, reason = args["route"], args["reason"]
            if not isinstance(code, str) or code not in nearby or not isinstance(reason, str):
                raise PlannerError("Invalid or previously searched route.")
            route = nearby.pop(code)
            found = cached_search(trip, route)
            followups.append(route)
            options.extend(found)
            routes.append((route.origin, route.destination))
            decisions.append(f"{route.code} · {reason.strip()[:280]}")
            matches = matching_options(trip, options)
            record_output(messages, call_id, observe(found))
    except (OpenAIError, PlannerError):
        baseline = search_recovery(
            trip, route_search=cached_search, prior_searches=tuple(followups), live=live,
            next_day=next_day,
        )
        return replace(
            baseline, planner_error=True, decisions=tuple(decisions),
            reason="The model planner was unavailable. TripRescue finished with the rule-based planner.",
        )

    return SearchResult(
        options, matches, routes, "The model selected follow-up searches using earlier results.",
        searched_dates=(None, *(route.outbound_date for route in followups)),
        decisions=tuple(decisions),
    )


def run_model_planner(trip: Trip, *, route_search=search_flights, live=False, next_day=None) -> SearchResult:
    with make_client() as client:
        return search_with_model(
            trip, client, model=os.getenv("TRIPRESCUE_MODEL") or DEFAULT_MODEL,
            route_search=route_search, live=live, next_day=next_day,
        )
