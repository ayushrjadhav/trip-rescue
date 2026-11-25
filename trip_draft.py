"""Extract proposed edits to a traveler-confirmed trip draft."""

import json
import os
from datetime import datetime

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


class DraftRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    draft: Draft = Field(default_factory=Draft)
    question: str | None = Field(None, max_length=300)


class DraftEdit(BaseModel):
    origin: str | None
    destination: str | None
    ready_at: str | None
    arrive_by: str | None
    budget: int | None
    checked_bag: bool | None
    clarification: str | None


QUESTIONS = {
    "origin": "Which airport can you depart from?",
    "destination": "Which airport do you need to reach?",
    "ready_at": "What is the earliest date and local time you can depart?",
    "arrive_by": "What is your latest arrival date and local time?",
    "budget": "What is your maximum budget in US dollars?",
    "checked_bag": "Will you need to check a bag?",
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
                "Do not guess which airport a multi-airport city refers to. Use one clarification "
                "question for ambiguity or a city destination that requires a transfer outside airport scope. "
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


def update_draft(request: DraftRequest, edit: DraftEdit, supported: dict[str, str]) -> dict:
    draft = request.draft.model_dump()
    for field, value in edit.model_dump(exclude={"clarification"}, exclude_none=True).items():
        draft[field] = value.strip() if isinstance(value, str) else value

    issue = None
    for field in ("origin", "destination"):
        code = draft[field]
        if code:
            code = code.upper()
            draft[field] = code if code in supported else None
            if draft[field] is None:
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
                    instants[field] = local_time_to_utc(local, AIRPORTS[airport].zone)
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
    question = issue or clarification or next(
        (prompt for field, prompt in QUESTIONS.items() if draft[field] is None), None
    )
    return {"draft": draft, "question": question}
