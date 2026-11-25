# TripRescue

TripRescue helps travelers compare replacement flights after a cancellation. The first milestone is a Python and Streamlit demo for one traveler using simulated US domestic flights and prices in USD.

## Run locally

With Python 3.10 or newer, run the following from the repository root.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Try the demo

1. Keep the default Chicago (ORD) to New York (LGA) trip and select **Find recovery options**.
2. Enter the earliest departure in the origin's local time and the deadline in the destination's local time.
3. Set a maximum total budget in whole US dollars and select the checked bag if needed.

The app compares each simulated flight's fare, taxes and fees, and optional bag fee against the budget. A flight qualifies if its total is at or below the budget and it arrives at or before the deadline. Qualifying flights are sorted by total cost, with earlier arrival breaking ties.

`app.py` handles the form and results. `recovery.py` contains the simulated schedules, local time conversion, validation, and ranking. The demo uses no API keys and makes no bookings.

## Nearby-airport search and fallback choices

TripRescue checks the original airports first. If none of those flights meet both limits, a rule-based search chooses which nearby airport pair to try next. It prioritizes estimated savings when the budget is the blocker and shorter ground transfers when timing is the blocker or both limits are tight. The search stops when it finds a qualifying route or runs out of supported airport pairs. The results show which routes it checked.

The simulator supports Chicago Midway, Newark, and Oakland as alternates for ORD, LGA, and SFO. A departure-side transfer pushes back the earliest available flight. An arrival-side transfer must finish by the deadline at the original destination airport. Both transfers count toward the total budget.

Try the default ORD–LGA trip with a **$160 budget** and no checked bag. With a **7:00 PM** arrival deadline, the search finds a **$159** itinerary via Chicago Midway. With a **6:00 PM** deadline, the app shows the cheapest on-time itinerary and the earliest in-budget itinerary, along with the exact change each requires. If neither limit can be kept, it shows the changes needed for the cheapest and earliest itineraries. Applying a choice updates the form and rechecks the original and nearby airports.

## OpenAI planner

The rules planner works without credentials. To enable the optional OpenAI planner, create an [OpenAI API key](https://platform.openai.com/api-keys) in a project with API billing enabled and set it as `OPENAI_API_KEY` in the terminal that launches Streamlit. Keep the key out of chat and Git. After installing the updated requirements, restart Streamlit and select **Use the OpenAI planner** before submitting a trip.

The default model is `gpt-4.1-mini`. Set `TRIPRESCUE_MODEL` to override it with a Responses API model that supports strict function calling. Opting in sends trip constraints and simulated search results to OpenAI.

Python checks the original route first. If it misses and nearby routes are available, the model calls `find_nearby_airports`, then chooses untried routes with `search_flights`, using each result to inform the next choice. Python limits the search to four unique routes and validates all costs and final arrival times. If the API fails or returns an invalid route, the rules planner completes the search. Only the traveler can approve a change to the trip limits.

Run the repeatable simulator comparison from the activated environment.

```sh
python evaluate.py          # Rules planner only
python evaluate.py --model  # Compare both planners using your API key
```

## Compare recovery options

Results highlight the lowest total and earliest final arrival among the routes checked. Use **Compare all nearby routes** to search every supported airport pair with the same limits. Each card shows the total, local departure and final arrival, transfer details, and the cost breakdown.

The benchmark includes 13 scenarios across several cities, including trips with multiple viable alternates, tight transfer windows, and checked bags. It reports recovery accuracy, routes searched, and the fare gap to the cheapest qualifying itinerary in an exhaustive search.

## Live flights with SerpApi

Set `SERPAPI_API_KEY` in the same terminal you use to launch Streamlit. On macOS with zsh, you can enter the key without echoing it or adding it to shell history.

```zsh
read -rs 'SERPAPI_API_KEY?Paste your SerpApi key: '
echo
export SERPAPI_API_KEY
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py
```

Select **Live Google Flights** in **Flight source** and search a future departure, such as ORD to LGA. Live mode requests fresh one-way economy fares for one adult in USD. It searches the first eligible local departure date per airport pair, up to four pairs, and reuses results within the same search when you compare routes or change a limit. Submit the form again to refresh the quotes. Nearby ground-transfer times and costs are estimates.

Airfare is shown as the provider's listed price. When you request a checked bag, fares without a confirmed bag price appear separately from verified budget matches. **Check sellers and baggage** uses an additional API request for the selected flight when SerpApi supplies a booking token. Use **Open Google Flights** to continue when a seller requires a form submission. TripRescue performs read-only searches and leaves booking to the traveler.

- [SerpApi Google Flights API](https://serpapi.com/google-flights-api)
- [SerpApi Google Flights Booking Options API](https://serpapi.com/google-flights-booking-options)

## Standalone web app for Vercel

The standalone app uses FastAPI and a browser frontend. It shares the recovery
engine, simulator, SerpApi integration, and optional OpenAI planner with the
Streamlit prototype. The top recommendation is the lowest verified total found.
Live offers link to Google Flights for booking and baggage confirmation.

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn web_app:app --reload
```

Open [the local web app](http://127.0.0.1:8000). Live flights are the default
when `SERPAPI_API_KEY` is set. The OpenAI planner is on by default when
`OPENAI_API_KEY` is set. You can change either in **Search settings**.
Export the keys in the terminal that starts Uvicorn, then restart it to pick
up changes.

For a Vercel preview, import this repository as a separate FastAPI project
using the repository root. The entry point is configured in `pyproject.toml`.
Set the provider keys as server-side Preview environment variables. Use a
protected preview when testing with live credentials.
