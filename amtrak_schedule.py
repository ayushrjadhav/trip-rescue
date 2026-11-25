"""Scheduled rail departures from Amtrak's public GTFS feed."""

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO, TextIOWrapper
from threading import Lock
from time import monotonic
from zipfile import ZipFile

import requests

FEED_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"
STATIONS = {"NYP": "Moynihan Train Hall at Penn Station", "WAS": "Washington Union Station"}
CACHE_SECONDS = 3600
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
RAIL_TYPES = {"2", "100", "101", "102", "103", "104", "105", "106", "107", "108", "109", "110", "111", "112", "113", "114", "115", "116", "117"}
_cache_lock = Lock()
_cached_feed = None


@dataclass(frozen=True)
class Schedule:
    trains: list
    calendar: dict
    exceptions: dict
    first_day: date
    last_day: date


def feed_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def gtfs_seconds(value: str) -> int:
    hour, minute, second = map(int, value.split(":"))
    return hour * 3600 + minute * 60 + second


def parse_feed(content: bytes) -> Schedule:
    with ZipFile(BytesIO(content)) as archive:
        def rows(name):
            if archive.getinfo(name).file_size > 8 * 1024 * 1024:
                raise ValueError("Schedule table is too large")
            with archive.open(name) as raw, TextIOWrapper(raw, encoding="utf-8-sig") as handle:
                yield from csv.DictReader(handle)

        calendar = {row["service_id"]: row for row in rows("calendar.txt")}
        exceptions = {
            (row["service_id"], row["date"]): row["exception_type"] == "1"
            for row in rows("calendar_dates.txt")
        } if "calendar_dates.txt" in archive.namelist() else {}
        info = next(rows("feed_info.txt"))
        routes = {row["route_id"]: row["route_long_name"] for row in rows("routes.txt")
                  if row["route_type"] in RAIL_TYPES}
        trips = {row["trip_id"]: row for row in rows("trips.txt") if row["route_id"] in routes}
        times = {}
        for row in rows("stop_times.txt"):
            if row["stop_id"] in STATIONS and row["trip_id"] in trips:
                times.setdefault(row["trip_id"], []).append(row)
        trains = []
        for trip_id, stops in times.items():
            ordered = sorted(stops, key=lambda row: int(row["stop_sequence"]))
            for departure, arrival in zip(ordered, ordered[1:]):
                if departure["stop_id"] == arrival["stop_id"]:
                    continue
                if departure.get("pickup_type") == "1" or arrival.get("drop_off_type") == "1":
                    continue
                trip = trips[trip_id]
                trains.append((
                    departure["stop_id"], arrival["stop_id"], gtfs_seconds(departure["departure_time"]),
                    gtfs_seconds(arrival["arrival_time"]), trip["service_id"],
                    routes[trip["route_id"]], trip.get("trip_short_name"),
                ))
        return Schedule(trains, calendar, exceptions, feed_date(info["feed_start_date"]),
                        feed_date(info["feed_end_date"]))


def load_feed() -> Schedule:
    global _cached_feed
    with _cache_lock:
        if _cached_feed and monotonic() < _cached_feed[0]:
            return _cached_feed[1]
        with requests.get(FEED_URL, timeout=(4, 10), stream=True) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Schedule archive is too large")
        schedule = parse_feed(bytes(content))
        _cached_feed = (monotonic() + CACHE_SECONDS, schedule)
        return schedule


def service_active(schedule: Schedule, service: str, day: date) -> bool:
    value = day.strftime("%Y%m%d")
    exception = schedule.exceptions.get((service, value))
    if exception is not None:
        return exception
    row = schedule.calendar.get(service)
    return bool(row and row["start_date"] <= value <= row["end_date"]
                and row[WEEKDAYS[day.weekday()]] == "1")


def scheduled_trains(schedule: Schedule, origin: str, destination: str,
                     ready: datetime, deadline: datetime, east) -> dict:
    first = ready.astimezone(east).date()
    last = min(deadline.astimezone(east).date(), first + timedelta(days=2))
    trips = []
    # The previous service day can include a departure after midnight.
    day = max(first - timedelta(days=1), schedule.first_day)
    while day <= min(last, schedule.last_day):
        service_start = datetime.combine(day, time(12), east).astimezone(timezone.utc) - timedelta(hours=12)
        for start, end, depart, arrive, service, route, number in schedule.trains:
            if (start, end) != (origin, destination) or not service_active(schedule, service, day):
                continue
            departure = service_start + timedelta(seconds=depart)
            arrival = service_start + timedelta(seconds=arrive)
            if ready <= departure < arrival <= deadline and departure.astimezone(east).date() <= last:
                trips.append({"route": route, "number": number, "origin": STATIONS[start],
                              "destination": STATIONS[end], "departure": departure, "arrival": arrival})
        day += timedelta(days=1)
    trips.sort(key=lambda trip: trip["departure"])
    return {"trips": trips[:16], "first_day": first, "last_day": last,
            "coverage_first": schedule.first_day, "coverage_last": schedule.last_day}
