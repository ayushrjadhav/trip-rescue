# TripRescue

TripRescue helps travelers find a replacement after a flight cancellation. Describe what happened in chat or enter a trip manually, then compare options against your arrival deadline and budget. The app supports one traveler and prices in USD.

[Open TripRescue](https://triprescue.ayushjadhav.com)

## Run locally

Use Python 3.12 or newer. From the repository root, run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn web_app:app --reload
```

Open [localhost:8000](http://127.0.0.1:8000). The manual form and simulator work without credentials. For live search or chat, export the corresponding keys in the terminal that starts Uvicorn. Restart Uvicorn after setting them.

| Environment variable | Enables |
| --- | --- |
| `SERPAPI_API_KEY` | Live one-way economy fares via SerpApi's Google Flights API. Live flights become the default when configured. |
| `OPENAI_API_KEY` | Ask TripRescue and the AI route planner. The planner is on by default when configured and can be switched off in **Search settings**. |
| `TRIPRESCUE_MODEL` | Optional model override for chat and planning. The default is `gpt-4.1-mini`. Use a Responses API model that supports structured outputs and strict function calling. |

On macOS with zsh, enter keys without echoing them:

```zsh
read -rs 'OPENAI_API_KEY?Paste your OpenAI API key: '
echo
export OPENAI_API_KEY

read -rs 'SERPAPI_API_KEY?Paste your SerpApi key: '
echo
export SERPAPI_API_KEY
```

Keep keys out of source control. [OpenAI API keys](https://platform.openai.com/api-keys) · [SerpApi Google Flights API](https://serpapi.com/google-flights-api)

## Recover a trip

- **Chat or manual entry.** Ask TripRescue fills in a trip draft, asks one question at a time, and searches once it has the required details. Follow-up messages update the draft. Click **Edit trip details** to make changes directly. Times are interpreted in the relevant airport's timezone. If a live departure is in the past, chat can offer a one-click correction when it fits the deadline.
- **Airport and date flexibility.** The live picker uses U.S. IATA airports and timezones from the bundled airport catalog. Coverage depends on the flight provider. The planner can try configured nearby airports and next-day departures when the deadline permits. **Any NYC airport** checks JFK, LaGuardia, and Newark. **Widen your search** lets you request a specific follow-up while keeping your original limits. Each recovery request makes at most four flight searches. [Airport catalog](https://pypi.org/project/airportsdata/)
- **Rank and compare.** Choose **Lowest total**, **Earliest arrival**, or **Fewest stops**. An earlier-arrival premium ranks options within that premium first while respecting the overall budget. **Compare two flights** appears when there are at least two matches or possible matches.
- **Transfers and risks.** Add a final destination to include the estimated airport transfer in both the total and final arrival. Manhattan has editable estimates from JFK, LaGuardia, and Newark. For other destinations, enter your own time and cost. Cards flag short connections, separate tickets, airport changes, and tight baggage rechecks when the itinerary data supports those notes.
- **Checked bags and fallbacks.** On-time flights within budget before an unknown bag fee appear under **Possible matches · Check bag fees**. Verify the fee with the airline and enter it to recalculate the total. If an itinerary requires a higher budget or a later deadline, the fallback card shows the proposed change before you apply it.
- **Save, share, and recheck.** Save one plan in your browser or copy a plain-text summary. **Recheck live fare** looks up the saved flight's route and date, reports price or schedule changes, and can show a same-route alternative when the original flight can't be identified. Chat history and the draft stay in the current browser session.

The desktop workspace displays chat beside the trip and results. On mobile, use **Chat** and **Results**, or tap **View results** after the search. TripRescue links out to Google Flights for booking.

## Train and bus alternatives

For eligible New York–Washington, DC searches in either direction, expand **Train and bus alternatives → Train · Amtrak → Find scheduled Amtrak trains**. The free public schedule feed shows up to 16 direct trains between Moynihan Train Hall and Washington Union Station across the first three departure days in your search window. It requires no API key. The results show scheduled station times. Confirm the fare and seats with Amtrak and allow time for station transfers. [Amtrak schedule feed](https://content.amtrak.com/content/gtfs/GTFS.zip)

Greyhound still uses an operator link and planning estimates. Enter the operator's quoted total and final arrival, including station transfers, to check them against your budget and deadline. Amtrak and Greyhound links open in a new tab.

## Verify locally

- **Simulator.** In the manual form, choose **Simulated flights**. Search ORD → LGA with 9 AM departure, 6 PM arrival deadline on the same day, a $350 budget, and no checked bag. The lowest-total option is $184, and **Compare two flights** is available. Set a $50 earlier-arrival premium to see the $231 option move to the top.
- **Chat.** With OpenAI enabled, try “My O’Hare flight was canceled. I can leave tomorrow at 6 AM Chicago time and need to reach New York by 10 AM Eastern. I can spend $450 and have a checked bag.” In live mode, the assistant offers NYC airport choices when an airport is unspecified. Selecting one should start a search once the draft is complete.
- **Rail.** With live flights configured, search JFK → DCA with a future departure and a wide arrival window, then load the Amtrak departures. The schedule endpoint itself works without any API keys.

Run the 13-scenario simulator benchmark:

```sh
.venv/bin/python evaluate.py
.venv/bin/python evaluate.py --model
```

The second command uses your OpenAI key to compare recovery accuracy and searches against the rules planner. The benchmark also reports the fare difference from the cheapest qualifying simulated option in an exhaustive search.

## Deploy to Vercel

Import the repository as a FastAPI project using the repository root. `pyproject.toml` declares the Vercel runtime dependencies and the `web_app:app` entry point.

Add provider keys as server-side environment variables for the deployment you're testing. Use **Preview** for branch previews and **Production** for the production site. Redeploy after changing variables. The existing production domain is `triprescue.ayushjadhav.com`. [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi) · [Environment variables](https://vercel.com/docs/environment-variables/managing-environment-variables)

## Project layout

| Path | Purpose |
| --- | --- |
| `web/` | Chat workspace, manual form, itinerary comparison, and saved plans |
| `web_app.py` | FastAPI entry point and endpoints for configuration, trip drafts, recovery, and Amtrak schedules |
| `trip_draft.py`, `planner.py` | Structured trip edits and bounded OpenAI route planning |
| `recovery.py`, `travel_options.py` | Flight simulation, budget and deadline checks, transfers, ranking, and ground pilot |
| `serpapi_flights.py`, `airport_catalog.py`, `amtrak_schedule.py` | Flight provider, U.S. airport catalog, and rail schedule feed |
| `evaluate.py` | Simulator benchmark |
| `app.py` | Original Streamlit prototype, runnable with `.venv/bin/python -m streamlit run app.py` |
