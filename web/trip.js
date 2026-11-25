const form = document.querySelector('#trip-form');
const inputs = form.elements;
const submit = form.querySelector('button[type=submit]');
const status = document.querySelector('#status');
const results = document.querySelector('#results');
const heading = document.querySelector('#results-heading');
const agentForm = document.querySelector('#agent-form');
const agentButton = agentForm.querySelector('button');
const agentStatus = document.querySelector('#agent-status');
const airportOptions = document.querySelector('#airport-options');
const labels = new Map();
const catalog = {};
const draftFields = ['origin', 'destination', 'ready_at', 'arrive_by', 'budget'];
const storageKey = 'triprescue-draft';
let draftState = null;
let modelAvailable = false;
let drafting = false;
let searching = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function money(amount) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 0,
  }).format(amount);
}

function minutesLabel(minutes) {
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return [hours && `${hours} hr`, remainder && `${remainder} min`].filter(Boolean).join(' ') || '0 min';
}

function localTime(value, timeZone) {
  return new Intl.DateTimeFormat('en-US', {
    timeZone, weekday: 'short', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
  }).format(new Date(value));
}

function action(label, handle, secondary = false) {
  const button = element('button', label, secondary ? 'secondary' : '');
  button.type = 'button';
  button.addEventListener('click', handle);
  return button;
}

function card(flight, { title, note, recommended = false } = {}) {
  const article = element('article', undefined, `panel card${recommended ? ' recommended' : ''}`);
  if (title) article.append(element('h3', title));
  const header = element('div', undefined, 'card-head');
  header.append(element('strong', flight.route.join(' → ')), element('span', money(flight.total), 'price'));
  article.append(header, element('p', [flight.name, ...flight.flight_numbers].join(' · '), 'hint'));
  if (note) article.append(element('p', note));
  if (flight.baggage === null) {
    article.append(element('p', 'Known total. Checked bag cost still needs confirmation.', 'warning'));
  }

  const times = element('div', undefined, 'times');
  for (const [label, value, zone] of [
    ['Flight departs', flight.departure, flight.departure_zone],
    ['Final arrival', flight.final_arrival, flight.destination_zone],
  ]) {
    const item = element('p', label, 'hint');
    item.append(element('strong', localTime(value, zone)));
    times.append(item);
  }
  article.append(times);

  if (flight.search_url) {
    const link = element('a', 'Open Google Flights', 'action');
    link.href = flight.search_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    article.append(link);
  }

  const details = element('details');
  details.append(element('summary', 'Fare and transfer details'));
  const costs = [`${flight.search_url ? 'Listed airfare' : 'Fare'} ${money(flight.fare)}`];
  if (!flight.search_url) costs.push(`Taxes and fees ${money(flight.taxes)}`);
  if (flight.baggage !== null && flight.baggage > 0) costs.push(`Checked bag ${money(flight.baggage)}`);
  details.append(element('p', costs.join(' · ')));
  for (const leg of flight.transfers) {
    details.append(element('p', `Estimated transfer ${leg.origin} → ${leg.destination} · ${minutesLabel(leg.minutes)} · ${money(leg.cost)}`));
  }
  if (flight.checked_at) {
    details.append(element('p', `Fare checked ${localTime(flight.checked_at, flight.departure_zone)}`, 'hint'));
  }
  article.append(details);
  return article;
}

function render(payload, trip) {
  if (payload.planner_error) results.append(element('p', payload.reason, 'warning'));
  const [best, ...others] = payload.matches;
  if (best) {
    // The deadline offset may differ from the arrival offset at a DST transition.
    const remaining = Math.floor((new Date(payload.deadline) - new Date(best.final_arrival)) / 60000);
    const timing = remaining > 0 ? `${minutesLabel(remaining)} before your deadline` : 'At your deadline';
    const unspent = trip.budget - best.total;
    const budget = unspent ? `${money(unspent)} under budget` : 'At your budget';
    results.append(card(best, {
      title: 'Recommended itinerary', note: `Lowest total found · ${timing} · ${budget}`, recommended: true,
    }));
    const fastest = others.reduce((selected, current) =>
      !selected || new Date(current.final_arrival) < new Date(selected.final_arrival) ? current : selected, null);
    if (fastest && new Date(fastest.final_arrival) < new Date(best.final_arrival)) {
      const earlier = Math.floor((new Date(best.final_arrival) - new Date(fastest.final_arrival)) / 60000);
      const extra = fastest.total - best.total;
      results.append(card(fastest, {
        title: 'Earlier arrival', note: `${minutesLabel(earlier)} earlier · ${extra ? money(extra) + ' more' : 'Same total'}`,
      }));
    }
    const remainingFlights = others.filter(flight => flight !== fastest || new Date(flight.final_arrival) >= new Date(best.final_arrival));
    if (remainingFlights.length) {
      const details = element('details');
      details.append(element('summary', `View ${remainingFlights.length} more matching ${remainingFlights.length === 1 ? 'flight' : 'flights'}`));
      const list = element('div', undefined, 'more');
      list.append(...remainingFlights.map(flight => card(flight)));
      details.append(list);
      results.append(details);
    }
  } else {
    results.append(element('h3', payload.fallbacks.length || payload.unverified_baggage.length
      ? 'No verified match within both limits' : 'No flights found'));
    for (const fallback of payload.fallbacks) {
      const changes = [];
      if (fallback.extra_cost) changes.push(`Increase your budget by ${money(fallback.extra_cost)}`);
      if (fallback.extra_minutes) changes.push(`Extend your deadline by ${minutesLabel(fallback.extra_minutes)}`);
      const panel = card(fallback.flight, { note: changes.join(' · ') });
      panel.append(action('Apply changes and search', () => {
        const updated = { ...trip, budget: fallback.budget, arrive_by: fallback.arrive_by, compare_all: true };
        for (const name of ['origin', 'destination', 'ready_at', 'arrive_by', 'budget', 'source']) {
          inputs[name].value = updated[name];
        }
        inputs.checked_bag.checked = updated.checked_bag;
        inputs.use_model.checked = updated.use_model;
        replaceAirportOptions();
        saveDraft({ draft: Object.fromEntries([...draftFields, 'checked_bag'].map(field => [field, updated[field]])),
          question: null, source: updated.source });
        search(updated);
      }));
      results.append(panel);
    }
    if (payload.unverified_baggage.length) {
      const details = element('details');
      details.append(element('summary', 'View fares with unverified bag fees'));
      const list = element('div', undefined, 'more');
      list.append(...payload.unverified_baggage.map(flight => card(flight)));
      details.append(list);
      results.append(details);
    }
  }
  if (payload.can_compare_all) {
    results.append(action('Compare more departures', () => search({ ...trip, compare_all: true }), true));
  }
  const history = element('details');
  const count = payload.searched.length;
  history.append(element('summary', `Search history · ${count} ${count === 1 ? 'search' : 'searches'}`));
  if (!payload.planner_error) history.append(element('p', payload.reason));
  const routes = element('ol');
  for (const [index, [origin, destination]] of payload.searched.entries()) {
    const date = payload.searched_dates?.[index];
    routes.append(element('li', `${labels.get(origin) || origin} → ${labels.get(destination) || destination}${date ? ' · ' + date : ''}`));
  }
  history.append(routes);
  for (const decision of payload.decisions) history.append(element('p', decision, 'hint'));
  results.append(history);
}

async function search(trip) {
  if (searching || drafting) return;
  searching = true;
  submit.disabled = true;
  agentButton.disabled = true;
  results.replaceChildren();
  status.textContent = 'Checking your flights. Nearby airports or next-day departures may be included.';
  results.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch('/api/recover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(trip),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Check the trip details and try again.');
    status.textContent = payload.source === 'live'
      ? 'Live fares from Google Flights. Prices can change before booking.'
      : 'Simulated flights · For demonstration';
    render(payload, trip);
  } catch (error) {
    status.textContent = 'Search unavailable';
    results.append(element('p', error.message || 'Try again in a moment.', 'warning'));
  } finally {
    searching = false;
    submit.disabled = false;
    agentButton.disabled = !modelAvailable;
    results.setAttribute('aria-busy', 'false');
    heading.focus();
  }
}

function airportCode(value) {
  const input = value.trim();
  const options = catalog[inputs.source.value] || [];
  return options.find(airport => airport.code === input.toUpperCase() || airport.label === input)?.code;
}

function indicateAirports() {
  for (const field of ['origin', 'destination']) {
    const code = airportCode(inputs[field].value);
    const airport = catalog[inputs.source.value]?.find(item => item.code === code);
    document.querySelector(`#${field}-airport`).textContent = airport ? `${airport.label} · ${airport.zone}` : '';
  }
}

function replaceAirportOptions() {
  airportOptions.replaceChildren(...(catalog[inputs.source.value] || []).map(airport =>
    new Option(airport.label, airport.code)));
  indicateAirports();
}

function saveDraft(state) {
  draftState = state;
  try { sessionStorage.setItem(storageKey, JSON.stringify(state)); } catch { /* Storage is optional. */ }
}

function applyDraft(draft) {
  for (const field of draftFields) inputs[field].value = draft[field] ?? '';
  inputs.checked_bag.checked = draft.checked_bag === true;
  indicateAirports();
}

function readDraft() {
  return {
    origin: airportCode(inputs.origin.value) || null,
    destination: airportCode(inputs.destination.value) || null,
    ready_at: inputs.ready_at.value || null,
    arrive_by: inputs.arrive_by.value || null,
    budget: Number(inputs.budget.value) || null,
    checked_bag: draftState?.draft.checked_bag === null ? null : inputs.checked_bag.checked,
  };
}

for (const field of ['origin', 'destination']) inputs[field].addEventListener('input', indicateAirports);
inputs.source.addEventListener('change', replaceAirportOptions);
inputs.checked_bag.addEventListener('change', () => {
  if (draftState) saveDraft({ ...draftState, draft: { ...draftState.draft, checked_bag: inputs.checked_bag.checked } });
});

agentForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (drafting || searching || !modelAvailable) return;
  const message = agentForm.elements.message.value.trim();
  if (!message) return;
  const source = inputs.source.value;
  drafting = true;
  agentButton.disabled = true;
  submit.disabled = true;
  agentStatus.textContent = 'Reading your trip details…';
  try {
    const response = await fetch('/api/trip-draft', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message, draft: draftState ? readDraft() : {}, question: draftState?.question,
        source,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check your message and try again.');
    if (inputs.source.value !== source) {
      agentStatus.textContent = 'Flight source changed. Submit your request again for the current source.';
      return;
    }
    saveDraft({ ...data, source });
    applyDraft(data.draft);
    agentForm.elements.message.value = '';
    agentForm.elements.message.placeholder = 'Add a detail or change a preference.';
    agentButton.textContent = 'Update trip draft';
    agentStatus.textContent = data.question || 'Your draft is ready. Review the trip below and select Find replacement flights.';
  } catch (error) {
    agentStatus.textContent = error.message;
  } finally {
    drafting = false;
    agentButton.disabled = false;
    submit.disabled = false;
  }
});

form.addEventListener('submit', event => {
  event.preventDefault();
  if (drafting || searching) return;
  const draft = readDraft();
  if (!draft.origin || !draft.destination) {
    status.textContent = 'Choose two airports from the suggestions or enter their three-letter codes.';
    heading.focus();
    return;
  }
  const values = new FormData(form);
  draft.checked_bag = values.has('checked_bag');
  saveDraft({ draft, question: null, source: values.get('source') });
  search({
    ...draft,
    source: values.get('source'), use_model: values.has('use_model'),
  });
});

async function initialize() {
  try {
    const response = await fetch('/api/config');
    if (!response.ok) throw new Error();
    const config = await response.json();
    catalog.simulated = config.airports;
    catalog.live = config.live_airports;
    for (const { code, label } of [...config.airports, ...config.live_airports]) labels.set(code, label);
    inputs.origin.value = 'ORD';
    inputs.destination.value = 'LGA';
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    const date = [tomorrow.getFullYear(), tomorrow.getMonth() + 1, tomorrow.getDate()]
      .map(part => String(part).padStart(2, '0')).join('-');
    inputs.ready_at.value = `${date}T09:00`;
    inputs.arrive_by.value = `${date}T18:00`;
    if (config.live_available) {
      inputs.source.add(new Option('Live Google Flights', 'live'), 0);
      inputs.source.value = 'live';
    } else {
      document.querySelector('#source-hint').textContent = 'Live flights need SERPAPI_API_KEY on the server.';
    }
    inputs.use_model.disabled = !config.model_available;
    inputs.use_model.checked = config.model_available;
    modelAvailable = config.model_available;
    agentButton.disabled = !modelAvailable;
    if (!modelAvailable) agentStatus.textContent = 'The assistant is unavailable. You can still fill in your trip below.';
    else {
      try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey));
        if (saved?.draft) {
          draftState = saved;
          if (saved.source === 'simulated' || (saved.source === 'live' && config.live_available)) inputs.source.value = saved.source;
          applyDraft(saved.draft);
          agentButton.textContent = 'Update trip draft';
          if (saved.question) agentStatus.textContent = saved.question;
        }
      } catch { /* An expired session does not prevent manual search. */ }
    }
    replaceAirportOptions();
    document.querySelector('#planner-hint').textContent = config.model_available
      ? 'OpenAI can choose a nearby airport or next-day departure when the first search misses. It receives trip constraints and search results.'
      : 'The standard planner is active. Set OPENAI_API_KEY on the server to enable the OpenAI planner.';
    submit.disabled = false;
    status.textContent = 'Enter your trip to see the best available options.';
  } catch {
    status.textContent = 'Could not load search settings. Refresh the page to try again.';
  }
}

initialize();
