"""Streamlit entry point for TripRescue."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import os
from pathlib import Path

import streamlit as st

from planner import run_model_planner
from recovery import (
    AIRPORTS, PRIMARY_AIRPORTS, RecoveryOption, SearchResult, SearchRoute, Trip,
    fallback_options, find_nearby_airports, local_time_to_utc, matching_options,
    search_flights, search_recovery, validation_errors,
)
from serpapi_flights import FlightProviderError, api_key, lookup_sellers, search_live


def airport_label(code: str) -> str:
    return f"{AIRPORTS[code].city} ({code})"


def local_display(value: datetime, airport: str) -> str:
    return value.astimezone(AIRPORTS[airport].zone).strftime("%a, %b %d · %I:%M %p %Z")


def duration_label(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if not hours:
        return f"{remainder} min"
    return f"{hours} hr {remainder} min" if remainder else f"{hours} hr"


def show_sellers(option: RecoveryOption) -> None:
    fare = option.live_fare
    if not fare.booking_token:
        return
    if st.button("Check sellers and baggage", key=f"sellers_{hash(option)}"):
        try:
            with st.spinner("Checking sellers…"):
                st.session_state.seller_details[option] = lookup_sellers(option)
        except FlightProviderError as error:
            st.warning(str(error))
    sellers = st.session_state.seller_details.get(option)
    if sellers == []:
        st.caption("No seller details were returned. Check the flight on Google Flights.")
    for seller in sellers or []:
        price = f" · ${seller.price:,}" if seller.price is not None else ""
        st.text(f"{seller.name}{price}")
        if seller.baggage:
            st.text(" · ".join(seller.baggage))
        if seller.url:
            st.link_button("Open seller offer", seller.url)
    if sellers and not all(seller.url for seller in sellers):
        st.caption("Use Google Flights to continue to sellers without a direct link.")


def show_option(
    option: RecoveryOption, trip: Trip, *, highlight: str = "",
    explanation: str = "", primary: bool = False,
) -> None:
    route, price = st.columns([3, 2])
    if highlight:
        route.caption(highlight)
    route.markdown(f"#### {' → '.join(option.route)}")
    route.caption(option.name)
    if option.live_fare and option.live_fare.flight_numbers:
        route.text(" · ".join(option.live_fare.flight_numbers))
    price.metric(
        "Total per traveler" if option.price_complete else "Known total before checked bag",
        f"${option.total:,}",
    )

    if explanation:
        st.write(explanation)
    departure, arrival = st.columns(2)
    departure.caption(f"Flight departs · {airport_label(option.route[0])}")
    departure.write(f"**{local_display(option.departure, option.route[0])}**")
    arrival.caption(f"Final arrival · {airport_label(trip.destination)}")
    arrival.write(f"**{local_display(option.final_arrival, trip.destination)}**")
    if option.live_fare:
        st.link_button(
            "Open Google Flights", option.live_fare.search_url,
            type="primary" if primary else "secondary",
        )
    with st.expander("Fare and transfer details"):
        for leg in (option.before_flight, option.after_flight):
            if leg:
                st.write(
                    f"Ground transfer {leg.origin} → {leg.destination} · "
                    f"{duration_label(leg.minutes)} · ${leg.cost}"
                )
        if option.after_flight:
            st.caption(
                f"Flight lands at {option.route[-1]} · "
                f"{local_display(option.arrival, option.route[-1])}"
            )
        if option.live_fare:
            bag = "Checked bag price unknown" if not option.price_complete else "No checked bag requested"
            st.caption(
                rf"Listed airfare \${option.fare:,} · {bag} "
                rf"· Estimated ground transfers \${option.ground_cost:,}"
            )
            st.caption(
                f"Google Flights via SerpApi · Checked "
                f"{local_display(option.live_fare.checked_at, trip.origin)}"
            )
            show_sellers(option)
        else:
            st.caption(
                rf"Fare \${option.fare:,} · Taxes and fees \${option.taxes:,} "
                rf"· Checked bag \${option.baggage:,} · Ground transfers \${option.ground_cost:,}"
            )


def cached_live_search(trip: Trip, route: SearchRoute) -> list[RecoveryOption]:
    inventory = st.session_state.live_inventory
    if route.code not in inventory:
        inventory[route.code] = search_live(trip, route, now=st.session_state.search_started_at)
    now = datetime.now(timezone.utc)
    return [option for option in inventory[route.code] if option.departure >= now]


def run_search(
    trip: Trip, *, use_model: bool = False, compare_all: bool = False,
    report: Callable[[str], None] | None = None,
) -> SearchResult:
    live = st.session_state.live_source
    provider = cached_live_search if live else search_flights

    def search_route(current: Trip, route: SearchRoute) -> list[RecoveryOption]:
        if report:
            message = f"Checking flights from {airport_label(route.origin)} to {airport_label(route.destination)}."
            if route.before or route.after:
                message += " Including the airport transfer in the total and arrival time."
            report(message)
        options = provider(current, route)
        if report and (route.origin, route.destination) == (current.origin, current.destination):
            if not matching_options(current, options) and find_nearby_airports(current):
                report("The original airports have no verified match. Looking at nearby airports.")
        return options

    if use_model:
        return run_model_planner(trip, route_search=search_route, live=live)
    return search_recovery(trip, include_nearby=compare_all, route_search=search_route, live=live)


def apply_limits(trip: Trip, option: RecoveryOption) -> None:
    updated = replace(
        trip,
        budget=max(trip.budget, option.total),
        arrive_by=max(trip.arrive_by, option.final_arrival),
    )
    try:
        result = run_search(updated, compare_all=True)
    except FlightProviderError as error:
        st.session_state.search_error = str(error)
        return
    if updated.budget != trip.budget:
        st.session_state.trip_budget = updated.budget
    if updated.arrive_by != trip.arrive_by:
        local = updated.arrive_by.astimezone(AIRPORTS[trip.destination].zone)
        st.session_state.trip_deadline_date = local.date()
        st.session_state.trip_deadline_time = local.time()
    st.session_state.active_trip = updated
    st.session_state.search_result = result
    st.session_state.search_notice = (
        f"Search updated · Budget ${updated.budget:,} · "
        f"Deadline {local_display(updated.arrive_by, updated.destination)}"
    )


def compare_all_routes(trip: Trip) -> None:
    try:
        st.session_state.search_result = run_search(trip, compare_all=True)
    except FlightProviderError as error:
        st.session_state.search_error = str(error)
        return
    st.session_state.search_notice = "Compared every supported nearby route using your current limits."


def show_fallbacks(trip: Trip, options: list[RecoveryOption]) -> None:
    st.subheader("No match within both limits")
    alternatives = fallback_options(trip, options)
    if alternatives:
        st.write("Choose which limit you can change to make a flight work.")
    for index, option in enumerate(alternatives):
        extra_cost = max(0, option.total - trip.budget)
        late_minutes = max(0, (option.final_arrival - trip.arrive_by) // timedelta(minutes=1))
        with st.container(border=True, key=f"fallback_{index}"):
            if extra_cost and late_minutes:
                st.markdown("**Change both limits**")
                button = "Update budget and deadline"
            elif extra_cost:
                st.markdown("**Keep your deadline**")
                button = "Update budget"
            else:
                st.markdown("**Keep your budget**")
                button = "Update deadline"
            if extra_cost:
                st.write(rf"Increase the budget by \${extra_cost:,} to \${option.total:,}.")
            if late_minutes:
                st.write(
                    f"Extend the deadline by {duration_label(late_minutes)} to "
                    f"{local_display(option.final_arrival, trip.destination)}."
                )
            show_option(option, trip)
            st.button(
                button, key=f"apply_fallback_{index}", type="primary",
                on_click=apply_limits, args=(trip, option),
            )


def show_matches(trip: Trip, result: SearchResult) -> None:
    recommended = result.matches[0]
    spare = (trip.arrive_by - recommended.final_arrival) // timedelta(minutes=1)
    extra_budget = trip.budget - recommended.total
    timing = f"{duration_label(spare)} before your deadline" if spare else "At your deadline"
    money = f"${extra_budget:,} under budget" if extra_budget else "At your budget"
    st.subheader("Recommended itinerary")
    with st.container(border=True, key="itinerary_recommended"):
        show_option(
            recommended, trip, highlight="Lowest total found",
            explanation=f"{timing} · {money}", primary=True,
        )

    others = result.matches[1:]
    featured = []
    if others:
        fastest = min(others, key=lambda option: option.final_arrival)
        if fastest.final_arrival < recommended.final_arrival:
            featured.append(fastest)
        simpler = min(others, key=lambda option: (len(option.route), option.total))
        if len(simpler.route) < len(recommended.route) and simpler not in featured:
            featured.append(simpler)
    if featured:
        st.subheader("Other ways to get there")
        for index, option in enumerate(featured):
            difference = option.total - recommended.total
            minutes = (recommended.final_arrival - option.final_arrival) // timedelta(minutes=1)
            cost_text = f"${difference:,} more" if difference else "Same total"
            time_text = (
                f"{duration_label(abs(minutes))} {'earlier' if minutes > 0 else 'later'}"
                if minutes else "Same arrival time"
            )
            highlight = "Fewer stops" if len(option.route) < len(recommended.route) else "Earlier arrival"
            with st.container(border=True, key=f"itinerary_alternative_{index}"):
                show_option(option, trip, highlight=highlight, explanation=f"{cost_text} · {time_text}")
    remaining = [option for option in others if option not in featured]
    if remaining:
        label = "View 1 more matching flight" if len(remaining) == 1 else f"View {len(remaining)} more matching flights"
        if st.toggle(label, key="show_all_matches"):
            for index, option in enumerate(remaining):
                with st.container(border=True, key=f"itinerary_more_{index}"):
                    show_option(option, trip)


def main() -> None:
    st.set_page_config(page_title="TripRescue", page_icon="✈️", layout="wide")
    st.html(Path(__file__).with_name("dashboard.css"))
    st.html("""
        <header class="trip-hero">
            <div class="trip-brand">TripRescue</div>
            <h1>Find another way there.</h1>
            <p>Compare replacement flights that fit your budget and arrival deadline.</p>
        </header>
    """)
    has_key = bool(os.getenv("OPENAI_API_KEY", "").strip())
    has_serpapi_key = bool(api_key())

    demo_date = datetime.now(AIRPORTS["ORD"].zone).date() + timedelta(days=1)
    for key, default in (
        ("trip_budget", 350), ("trip_deadline_date", demo_date), ("trip_deadline_time", time(18))
    ):
        st.session_state.setdefault(key, default)

    has_results = st.session_state.get("active_trip") is not None
    with st.expander("Edit trip" if has_results else "Your trip", expanded=not has_results), st.form("trip"):
        left, right = st.columns(2)
        origin = left.selectbox("Departure airport", PRIMARY_AIRPORTS, format_func=airport_label)
        destination = right.selectbox(
            "Destination airport", PRIMARY_AIRPORTS, index=1, format_func=airport_label
        )
        ready_date = left.date_input("Earliest departure date", value=demo_date)
        deadline_date = right.date_input("Arrival deadline date", key="trip_deadline_date")
        ready_time = left.time_input(
            "Earliest departure time", value=time(9), step=60,
            help="Use the local time at your departure airport.",
        )
        deadline_time = right.time_input(
            "Arrival deadline time", key="trip_deadline_time", step=60,
            help="Use the local time at your destination airport.",
        )
        budget = left.number_input(
            "Maximum total budget (USD)", min_value=0, step=1, key="trip_budget"
        )
        checked_bag = right.checkbox("Include one checked bag")
        if not has_serpapi_key:
            st.caption("Demo mode · Flight schedules and prices are simulated.")
        with st.popover("Search settings"):
            sources = ("Live Google Flights", "Simulated flights") if has_serpapi_key else ("Simulated flights",)
            source = st.radio("Flight source", sources, horizontal=True)
            live = source == "Live Google Flights"
            use_model = st.checkbox(
                "Use the OpenAI planner",
                disabled=not has_key,
                help=(
                    "Sends trip constraints and flight search results to OpenAI."
                    if has_key else "Set OPENAI_API_KEY before launching Streamlit to enable this mode."
                ),
            )
        submitted = st.form_submit_button("Find recovery options", type="primary")

    if submitted:
        st.session_state.active_trip = None
        st.session_state.search_notice = ""
        st.session_state.search_error = ""
        instants = []
        for label, day, clock, airport in (
            ("Earliest departure", ready_date, ready_time, origin),
            ("Arrival deadline", deadline_date, deadline_time, destination),
        ):
            try:
                instants.append(
                    local_time_to_utc(datetime.combine(day, clock), AIRPORTS[airport].zone)
                )
            except ValueError as error:
                st.error(f"{label} · {error}")
        if len(instants) != 2:
            return

        ready_at, arrive_by = instants
        trip = Trip(origin, destination, ready_at, arrive_by, budget, checked_bag)
        errors = validation_errors(trip)
        if errors:
            for error in errors:
                st.error(error)
            return
        now = datetime.now(timezone.utc)
        if live and trip.arrive_by <= now:
            st.error("Choose an arrival deadline in the future for live flights.")
            return
        st.session_state.live_source = live
        st.session_state.search_started_at = now
        st.session_state.live_inventory = {}
        st.session_state.seller_details = {}
        with st.status("Looking for flights", expanded=True) as progress:
            def report(message: str) -> None:
                progress.update(label=message)
                progress.write(message)

            try:
                result = run_search(trip, use_model=use_model and has_key, report=report)
            except FlightProviderError as error:
                progress.update(label="Search interrupted", state="error", expanded=False)
                st.error(str(error))
                return
            progress.update(label="Flight search complete", state="complete", expanded=False)
        st.session_state.search_result = result
        st.session_state.active_trip = trip
        st.rerun()

    trip = st.session_state.get("active_trip")
    if trip is None:
        return
    result = st.session_state.search_result
    live = st.session_state.live_source
    if error := st.session_state.pop("search_error", None):
        st.error(error)
    st.markdown(f"**{airport_label(trip.origin)} to {airport_label(trip.destination)}**")
    st.caption(
        f"Budget ${trip.budget:,} · Arrive by {local_display(trip.arrive_by, trip.destination)}"
    )
    if live:
        st.caption(
            "Live fares from Google Flights · Prices can change before booking."
        )
    else:
        st.caption("Simulated results · For demonstration")
    if notice := st.session_state.get("search_notice"):
        st.success(notice)
    if result.planner_error:
        st.warning(result.reason)
    if not result.matches:
        if live and not result.options:
            st.subheader("No flights found")
            st.write("Google Flights returned no usable flights for the routes and departure dates checked.")
        else:
            show_fallbacks(trip, result.options)
        unpriced = sorted(
            (option for option in result.options if not option.price_complete),
            key=lambda option: (
                option.final_arrival > trip.arrive_by, option.total > trip.budget,
                option.total, option.final_arrival,
            ),
        )
        if unpriced:
            st.subheader("Options with unverified bag fees")
            st.caption("Check sellers for bag prices before relying on these totals.")
            for index, option in enumerate(unpriced[:5]):
                with st.container(border=True, key=f"itinerary_unpriced_{index}"):
                    show_option(option, trip)
    else:
        show_matches(trip, result)
        if len(result.routes) < 1 + len(find_nearby_airports(trip)):
            st.button(
                "Compare all nearby routes", on_click=compare_all_routes, args=(trip,),
                help="Check every supported nearby route with the same budget and deadline.",
            )

    count = len(result.routes)
    route_label = "route" if count == 1 else "routes"
    with st.expander(f"Search history · {count} {route_label} checked"):
        if not result.planner_error:
            st.write(result.reason)
        for origin, destination in result.routes:
            st.write(f"{airport_label(origin)} to {airport_label(destination)}")
        for decision in result.decisions:
            st.text(decision)


if __name__ == "__main__":
    main()
