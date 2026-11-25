"""Web recovery preferences and the New York–Washington ground pilot."""

from typing import Literal

from pydantic import BaseModel, Field

from recovery import GroundTransfer, RecoveryOption

Priority = Literal["lowest_total", "earliest_arrival", "fewest_stops"]
NYC = {"JFK", "LGA", "EWR", "NYC"}
DC = {"DCA", "IAD", "BWI"}
MANHATTAN_ESTIMATES = {"JFK": (75, 15), "LGA": (60, 5), "EWR": (80, 25)}


class TravelPreferences(BaseModel):
    final_destination: str | None = Field(None, max_length=100)
    transfer_minutes: int | None = Field(None, strict=True, ge=0, le=1440)
    transfer_cost: int | None = Field(None, strict=True, ge=0, le=10000)
    priority: Priority = "lowest_total"
    extra_for_speed: int | None = Field(None, strict=True, ge=0, le=10000)


def final_transfer(preferences: TravelPreferences, airport: str) -> GroundTransfer | None:
    destination = preferences.final_destination
    if not destination:
        return None
    minutes, cost = preferences.transfer_minutes, preferences.transfer_cost
    if (minutes is None) != (cost is None):
        raise ValueError("Enter both transfer time and cost, or leave both blank for Manhattan estimates.")
    if minutes is None or cost is None:
        if destination.casefold() != "manhattan" or airport not in MANHATTAN_ESTIMATES:
            raise ValueError("Enter an estimated transfer time and cost for your final destination.")
        minutes, cost = MANHATTAN_ESTIMATES[airport]
    return GroundTransfer(airport, destination, minutes, cost)


def rank_options(options: list[RecoveryOption], preferences: TravelPreferences) -> list[RecoveryOption]:
    if not options or preferences.priority == "lowest_total":
        return options
    if preferences.priority == "fewest_stops":
        return sorted(options, key=lambda option: (len(option.route), option.total, option.final_arrival))
    limit = preferences.extra_for_speed
    cheapest = min(option.total for option in options)
    return sorted(options, key=lambda option: (
        limit is not None and option.total > cheapest + limit,
        option.final_arrival, option.total,
    ))


def ground_services(origin: str, destination: str) -> list[dict]:
    if origin in NYC and destination in DC:
        cities, slug = ("New York", "Washington, DC"), "new-york-ny-washington-dc"
    elif origin in DC and destination in NYC:
        cities, slug = ("Washington, DC", "New York"), "washington-dc-new-york-ny"
    else:
        return []
    return [
        {"mode": "Train", "provider": "Amtrak", "cities": cities,
         "url": "https://www.amtrak.com/northeastregional", "typical_minutes": 225},
        {"mode": "Bus", "provider": "Greyhound", "cities": cities,
         "url": f"https://www.greyhound.com/bus-routes/{slug}", "typical_minutes": 300},
    ]
