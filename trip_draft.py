"""Extract proposed edits to a traveler-confirmed trip draft."""

import json
import os
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, StrictBool

from planner import DEFAULT_MODEL, make_client
from recovery import AIRPORTS, local_time_to_utc


class Draft(BaseModel):
    origin: str | None = Field(None, max_length=3)
    destination: str | None = Field(None, max_length=3)
    ready_at: str | None = Field(None, max_length=16)
    arrive_by: str | None = Field(None, max_length=16)
    budget: int | None = Field(None, strict=True, gt=0)
    checked_bag: StrictBool | None = None
    nyc_flexible: StrictBool = False
    arrival_scope: Literal["airport", "beyond_airport"] | None = None


class DraftRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    draft: Draft = Field(default_factory=Draft)
    question: str | None = Field(None, max_length=300)
    airport_choice: str | None = Field(None, max_length=3)
    use_suggested_departure: StrictBool = False


class DraftEdit(BaseModel):
    origin: str | None
    destination: str | None
    ready_at: str | None
    arrive_by: str | None
    budget: int | None
    checked_bag: bool | None
    clarification: str | None
    nyc_flexible: bool | None = None
    arrival_scope: Literal["airport", "beyond_airport"] | None = None


QUESTIONS = {
    "origin": "Which airport can you depart from?",
    "destination": "Which airport do you need to reach?",
    "ready_at": "What is the earliest date and local time you can depart?",
    "arrive_by": "What is your latest arrival date and local time?",
    "budget": "What is your maximum budget in US dollars?",
    "checked_bag": "Will you need to check a bag?",
}

NEW_YORK_AIRPORTS = {
    "JFK": "John F. Kennedy (JFK)",
    "LGA": "LaGuardia (LGA)",
    "EWR": "Newark (EWR)",
}


def extract_edit(request: DraftRequest, now: datetime) -> DraftEdit | None:
    local_times = {zone.key: now.astimezone(zone).isoformat(timespec="minutes")
                   for zone in {airport.zone for airport in AIRPORTS.values()}}
    with make_client() as client:
        response = client.responses.parse(
            model=os.getenv("TRIPRESCUE_MODEL") or DEFAULT_MODEL,
            store=False, max_output_tokens=450,
            instructions=(
                "Extract only explicit changes to a US flight recovery draft. Return null for "
                "unchanged or unknown fields. Use three-letter IATA codes for unambiguous airports. "
                "Do not guess which airport a multi-airport city refers to. For New York City "
                "without a specific airport, return NYC and leave the airport-selection question to the app. Use one clarification "
                "question for ambiguity or a city destination that requires a transfer outside airport scope. "
                "Set nyc_flexible true only when the traveler explicitly accepts any NYC airport. "
                "Set arrival_scope to airport when the traveler confirms their deadline is at the airport, "
                "or beyond_airport if they specify a place like Manhattan. Retain a previously confirmed "
                "scope unless explicitly changed. Leave airport-versus-city questions to the app. "
                "Give times as YYYY-MM-DDTHH:MM in the relevant airport's time zone, using the supplied "
                "clock for relative dates. Do not infer a date or AM/PM when genuinely ambiguous. "
                "Use USD whole dollars. Resolve relative budget changes against the existing draft. "
                "Never relax or change any other preference, search flights, or claim to have booked. "
                "The prior question may contextualize a short answer."
            ),
            input=json.dumps({**request.model_dump(), "current_time_by_zone": local_times}),
            text_format=DraftEdit,
        )
    return response.output_parsed


def update_draft(request: DraftRequest, edit: DraftEdit, supported: dict[str, str], *, now: datetime | None = None) -> dict:
    draft = request.draft.model_dump()
    for field, value in edit.model_dump(exclude={"clarification"}, exclude_none=True).items():
        draft[field] = value.strip() if isinstance(value, str) else value

    issue = None
    choices = []
    live_nyc = all(code in supported for code in NEW_YORK_AIRPORTS)
    for field in ("origin", "destination"):
        code = draft[field]
        if code:
            code = code.upper()
            if field == "destination" and code == "NYC" and draft["nyc_flexible"] and live_nyc:
                draft[field] = code
                continue
            draft[field] = code if code in supported else None
            if draft[field] is None:
                if code == "NYC":
                    direction = "leave from" if field == "origin" else "fly into"
                    question = f"Which New York–area airport can you {direction}?"
                    if not issue:
                        choices = [{"code": airport, "label": label} for airport, label in NEW_YORK_AIRPORTS.items()
                                   if airport in supported]
                        if field == "destination" and live_nyc:
                            choices.append({"code": "NYC", "label": "Any NYC airport"})
                        issue = question
                    draft[field] = "NYC"
                else:
                    issue = issue or f"{code} is unavailable for this search mode. {QUESTIONS[field]}"

    instants = {}
    for field, airport_field in (("ready_at", "origin"), ("arrive_by", "destination")):
        value = draft[field]
        airport = draft[airport_field]
        if value:
            try:
                local = datetime.strptime(value, "%Y-%m-%dT%H:%M")
                draft[field] = local.isoformat(timespec="minutes")
                if airport:
                    instants[field] = local_time_to_utc(local, AIRPORTS["LGA" if airport == "NYC" else airport].zone)
            except (ValueError, OverflowError):
                draft[field] = None
                issue = issue or QUESTIONS[field]
    if draft["origin"] and draft["origin"] == draft["destination"]:
        issue = issue or "Which different airport should we use as your destination?"
    if len(instants) == 2 and instants["arrive_by"] <= instants["ready_at"]:
        issue = issue or "What arrival deadline should we use? It must be after your earliest departure."
    if draft["budget"] is not None and draft["budget"] <= 0:
        draft["budget"] = None
        issue = issue or QUESTIONS["budget"]

    # A clarification takes precedence over a missing field because it explains the traveler's ambiguity.
    clarification = (edit.clarification or "").strip()[:300]
    destination = draft["destination"]
    if (
        destination in supported and edit.arrival_scope != "beyond_airport"
        and re.search(rf"\b{re.escape(destination)}\s+airport\b", request.message, re.IGNORECASE)
    ):
        draft["arrival_scope"] = "airport"
    if draft["arrival_scope"] == "airport" and re.search(
        r"\bairport\b.+\b(?:manhattan|city|final destination)\b", clarification, re.IGNORECASE
    ):
        clarification = ""
    if draft["arrival_scope"] == "beyond_airport":
        clarification = (
            "What time should your flight reach the airport? Allow enough time afterward "
            "to get to your final destination."
        )
        if request.question == clarification and edit.arrive_by and edit.arrival_scope is None:
            draft["arrival_scope"] = "airport"
            clarification = ""
    correction = None
    if now and draft["origin"] in supported and (ready := instants.get("ready_at")) and ready < now:
        zone = AIRPORTS[draft["origin"]].zone
        local_now = now.astimezone(zone)
        today = datetime.combine(local_now.date(), ready.astimezone(zone).time())
        if today.replace(tzinfo=zone) < local_now:
            today = (local_now + timedelta(minutes=1)).replace(second=0, microsecond=0, tzinfo=None)
        if not instants.get("arrive_by") or today.replace(tzinfo=zone) < instants["arrive_by"]:
            correction = today.isoformat(timespec="minutes")
        if request.use_suggested_departure and correction:
            draft["ready_at"] = correction
            correction = None
            issue = None if issue == "What arrival deadline should we use? It must be after your earliest departure." else issue
        else:
            issue = issue or "That departure time has already passed. When can you leave?"

    question = issue or next(
        (prompt for field, prompt in QUESTIONS.items() if draft[field] is None), None
    ) or clarification or None
    if draft["destination"] != "NYC":
        draft["nyc_flexible"] = False
    return {"draft": draft, "question": question, "choices": choices if not clarification else [],
            "departure_correction": correction}
