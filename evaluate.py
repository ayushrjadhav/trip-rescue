"""Compare recovery outcomes and route counts on repeatable simulated trips."""

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os

from planner import run_model_planner
from recovery import Trip, fallback_options, search_recovery

READY = datetime(2026, 11, 22, 15, tzinfo=timezone.utc)
BASE_TRIP = Trip("ORD", "LGA", READY, READY + timedelta(hours=8), 350, False)


def scenario(origin, destination, budget, minutes, *, checked_bag=False):
    return Trip(origin, destination, READY, READY + timedelta(minutes=minutes), budget, checked_bag)


CASES = (
    ("Original route qualifies", BASE_TRIP, True),
    ("Nearby airport recovers trip", replace(BASE_TRIP, budget=160, arrive_by=READY + timedelta(hours=9)), True),
    ("Ground transfer misses deadline", replace(BASE_TRIP, budget=149, arrive_by=READY + timedelta(hours=9)), False),
    ("Single-limit fallbacks", replace(BASE_TRIP, budget=160), False),
    ("Both limits need to change", replace(BASE_TRIP, budget=1, arrive_by=READY + timedelta(minutes=1)), False),
    ("Chicago to Bay Area, either alternate works", scenario("ORD", "SFO", 325, 405), True),
    ("Chicago to Bay Area, only two transfers work", scenario("ORD", "SFO", 300, 450), True),
    ("Bay Area to Chicago, three viable routes with a bag", scenario("SFO", "ORD", 365, 450, checked_bag=True), True),
    ("New York to Bay Area, arrival-side alternate only", scenario("LGA", "SFO", 340, 495), True),
    ("New York to Bay Area, dual alternates with a bag", scenario("LGA", "SFO", 240, 720, checked_bag=True), True),
    ("Bay Area to New York, departure-side alternate only", scenario("SFO", "LGA", 340, 495), True),
    ("Chicago to Denver, one supported alternate", scenario("ORD", "DEN", 290, 285), True),
    ("Atlanta to Denver, no alternate airports", scenario("ATL", "DEN", 100, 480), False),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="store_true", help="Also run the OpenAI planner.")
    args = parser.parse_args()
    if args.model and not os.getenv("OPENAI_API_KEY", "").strip():
        parser.error("--model requires OPENAI_API_KEY.")
    planners = {"Rules": search_recovery}
    if args.model:
        planners["OpenAI"] = run_model_planner

    failed = False
    route_counts = {}
    reference = {
        label: search_recovery(trip, include_nearby=True).matches
        for label, trip, _ in CASES
    }
    for name, search in planners.items():
        print(f"\n{name}")
        searches = recovered = correct = errors = 0
        route_counts[name] = []
        for label, trip, expected_match in CASES:
            result = search(trip)
            valid = all(
                option.total <= trip.budget and option.final_arrival <= trip.arrive_by
                for option in result.matches
            )
            bounded = 1 <= len(result.routes) <= 4 and len(result.routes) == len(set(result.routes))
            passed = valid and bounded and bool(result.matches) == expected_match
            searches += len(result.routes)
            route_counts[name].append(len(result.routes))
            recovered += bool(result.matches)
            correct += passed
            errors += result.planner_error
            fallbacks = 0 if result.matches else len(fallback_options(trip, result.options))
            status = "rule fallback" if result.planner_error else "pass" if passed else "FAIL"
            best = f", best found ${result.matches[0].total}" if result.matches else ""
            if result.matches and reference[label]:
                gap = result.matches[0].total - reference[label][0].total
                best += f" (${gap} above exhaustive best)" if gap else " (exhaustive best)"
            print(f"  {label}: {status}, {len(result.routes)} routes, {fallbacks} fallback choices{best}")
        print(f"  Recovered {recovered} trips · {correct}/{len(CASES)} correct · {searches} route searches")
        if args.model and name == "OpenAI":
            print(f"  Model runs that fell back to rules: {errors}")
        failed |= correct != len(CASES) or bool(errors)
    if args.model:
        pairs = list(zip(route_counts["OpenAI"], route_counts["Rules"]))
        print(
            f"\nOpenAI route count vs rules: {sum(a < b for a, b in pairs)} fewer, "
            f"{sum(a == b for a, b in pairs)} tied, {sum(a > b for a, b in pairs)} more."
        )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
