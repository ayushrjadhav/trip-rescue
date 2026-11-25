"""U.S. airports with IATA codes and verified timezone identifiers."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import airportsdata

from recovery import AIRPORTS, Airport


def load_airports() -> dict[str, str]:
    labels = {}
    for code, record in sorted(airportsdata.load("IATA").items()):
        if record["country"] != "US" or len(code) != 3:
            continue
        try:
            zone = ZoneInfo(record["tz"])
        except (ValueError, ZoneInfoNotFoundError):
            continue
        city = record["city"] or record["subd"] or record["name"]
        AIRPORTS.setdefault(code, Airport(city, zone))
        labels[code] = f"{code} — {city}, {record['subd']} · {record['name']}"
    return labels
