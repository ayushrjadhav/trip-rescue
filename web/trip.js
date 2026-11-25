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
let activeSearch = null;
const workspace = document.querySelector('.workspace');
const conversation = document.querySelector('#conversation');
const choices = document.querySelector('#airport-choices');
const summary = document.querySelector('#trip-summary');
const transcript = [];
const resultLoading = document.querySelector('#result-loading');
let typingIndicator = null;

function setActivity(phase = '', showChat = true) {
  typingIndicator?.remove();
  typingIndicator = null;
  resultLoading.hidden = !phase;
  agentStatus.classList.toggle('visually-hidden', Boolean(phase && showChat));
  agentStatus.textContent = phase && showChat ? phase : '';
  if (!phase || !showChat) return;
  typingIndicator = element('div', undefined, 'chat-message assistant typing');
  typingIndicator.setAttribute('aria-hidden', 'true');
  const dots = element('span', undefined, 'typing-dots');
  dots.append(element('i'), element('i'), element('i'));
  typingIndicator.append(dots, element('span', phase));
  conversation.append(typingIndicator);
  conversation.scrollTop = conversation.scrollHeight;
}

function setMode(mode) {
  workspace.dataset.mode = mode;
  document.querySelector('#chat-mode').setAttribute('aria-pressed', mode === 'chat');
  document.querySelector('#manual-mode').setAttribute('aria-pressed', mode === 'manual');
}
for (const mode of ['chat', 'manual']) {
  document.querySelector(`#${mode}-mode`).addEventListener('click', () => setMode(mode));
}
for (const view of ['chat', 'results']) {
  document.querySelector(`#${view}-view`).addEventListener('click', () => {
    workspace.dataset.view = view;
    document.querySelector('#chat-view').setAttribute('aria-pressed', view === 'chat');
    document.querySelector('#results-view').setAttribute('aria-pressed', view === 'results');
  });
}

function chatMessage(role, message) {
  transcript.push({ role, message });
  const entry = element('div', undefined, `chat-message ${role}`);
  entry.append(element('strong', role === 'user' ? 'You' : 'TripRescue'), element('p', message));
  conversation.append(entry);
  conversation.scrollTop = conversation.scrollHeight;
}

function showDraft(state) {
  const draft = state.draft;
  summary.replaceChildren(element('h3', 'Trip details'));
  const origin = draft.origin || 'Departure airport needed';
  const destination = draft.destination === 'NYC'
    ? (draft.nyc_flexible ? 'Any NYC airport' : 'New York airport to confirm')
    : draft.destination || 'Destination airport needed';
  summary.append(element('p', `${origin} → ${destination}`));
  for (const [text, value] of [['Earliest departure', draft.ready_at], ['Arrive by', draft.arrive_by]]) {
    if (value) summary.append(element('p', `${text} · ${localTime(value + 'Z', 'UTC').replace(/ UTC$/, '')} airport local time`, 'hint'));
  }
  if (draft.budget) summary.append(element('p', `Up to ${money(draft.budget)}`, 'hint'));
  if (draft.checked_bag !== null) summary.append(element('p', draft.checked_bag ? 'One checked bag' : 'No checked bag', 'hint'));
  if (draft.destination === 'NYC') summary.append(element('p', 'Arrival deadline is at the airport.', 'hint'));
  summary.hidden = false;
  summary.append(action('Edit trip details', () => {
    applyDraft(draft);
    setMode('manual');
    inputs.origin.focus();
  }, true));
  choices.replaceChildren(...(state.choices || []).map(option =>
    action(option.label, () => sendMessage(option.label, option.code), true)));
  if (state.departure_correction) {
    const label = `Use ${localTime(state.departure_correction + 'Z', 'UTC').replace(/ UTC$/, '')} airport local time`;
    choices.append(action(label, () => sendMessage(label, undefined, true), true));
  }
}

document.querySelector('#reset-chat').addEventListener('click', () => {
  if (drafting) return;
  activeSearch?.abort();
  draftState = null;
  transcript.length = 0;
  conversation.replaceChildren();
  setActivity();
  choices.replaceChildren();
  summary.hidden = true;
  results.replaceChildren();
  status.textContent = 'Enter your trip to see the best available options.';
  try { sessionStorage.removeItem(storageKey); } catch { /* Storage is optional. */ }
});


function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function money(amount) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 2,
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

function card(flight, { title, note, recommended = false, budget, fees, refreshCompare } = {}) {
  const article = element('article', undefined, `panel card${recommended ? ' recommended' : ''}`);
  if (title) article.append(element('h3', title));
  const header = element('div', undefined, 'card-head');
  header.append(element('strong', flight.route.join(' → ')), element('span', money(flight.total), 'price'));
  article.append(header, element('p', [flight.name, ...flight.flight_numbers].join(' · '), 'hint'));
  if (note) article.append(element('p', note));
  if (flight.baggage === null) {
    article.append(element('p', 'Price shown excludes the checked bag. Verify the fee before booking.', 'warning'));
    const feeLabel = element('label', 'Checked-bag fee you confirmed (USD)', 'bag-fee');
    const fee = element('input');
    fee.type = 'number';
    fee.min = '0';
    fee.step = '0.01';
    fee.placeholder = 'Enter airline fee';
    const updated = element('p', '', 'hint');
    updated.setAttribute('aria-live', 'polite');
    feeLabel.append(fee);
    article.append(feeLabel, updated);
    fee.addEventListener('input', () => {
      if (!fee.value || !fee.validity.valid || !Number.isFinite(fee.valueAsNumber)) {
        fees.delete(flight);
        updated.textContent = '';
      } else {
        fees.set(flight, fee.valueAsNumber);
        const total = Math.round((flight.total + fee.valueAsNumber) * 100) / 100;
        updated.textContent = `${money(total)} including your bag · ${total <= budget ? 'Within budget' : money(total - budget) + ' over budget'}`;
      }
      refreshCompare();
    });
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

function flightComparison(flights, trip, fees) {
  const panel = element('details', undefined, 'panel flight-comparison');
  panel.append(element('summary', 'Compare two flights'));
  const controls = element('div', undefined, 'compare-columns');
  const selectors = [0, 1].map(index => {
    const label = element('label', index === 0 ? 'First flight' : 'Second flight');
    const select = element('select');
    flights.forEach((flight, i) => select.add(new Option(
      `${flight.route.join(' → ')} · ${money(flight.total)} · ${localTime(flight.final_arrival, flight.destination_zone)}`, i)));
    select.selectedIndex = index;
    label.append(select);
    controls.append(label);
    return select;
  });
  const comparison = element('div');
  panel.append(controls, comparison);
  function update() {
    const [first, second] = selectors.map(select => flights[Number(select.value)]);
    const columns = element('div', undefined, 'compare-columns');
    const knownTotal = flight => flight.total + (fees.get(flight) ?? 0);
    const incomplete = flight => flight.baggage === null && !fees.has(flight);
    for (const flight of [first, second]) {
      const column = element('div', undefined, 'compare-flight');
      const stops = flight.route.length - 2;
      const transfers = flight.transfers.map(leg => `${leg.origin} → ${leg.destination} · ${minutesLabel(leg.minutes)} · ${money(leg.cost)}`);
      column.append(
        element('strong', flight.route.join(' → ')),
        element('p', `${money(knownTotal(flight))}${incomplete(flight) ? ' before bag fee' : ''}`),
        element('p', `Arrives ${localTime(flight.final_arrival, flight.destination_zone)}`),
        element('p', stops ? `${stops} ${stops === 1 ? 'stop' : 'stops'}` : 'Nonstop'),
        element('p', transfers.length ? transfers.join(' / ') : 'No airport transfers'),
        element('p', incomplete(flight) ? `Bag fee unverified · ${money(trip.budget - flight.total)} left`
          : fees.has(flight) ? `Bag fee entered · ${money(fees.get(flight))}` : 'Bag costs accounted for'),
      );
      columns.append(column);
    }
    const difference = Math.round((knownTotal(first) - knownTotal(second)) * 100) / 100;
    const minutes = Math.round((Date.parse(first.final_arrival) - Date.parse(second.final_arrival)) / 60000);
    const cost = difference ? `${money(Math.abs(difference))} ${difference < 0 ? 'cheaper' : 'more'}` : 'the same price';
    const time = minutes ? `${minutesLabel(Math.abs(minutes))} ${minutes < 0 ? 'earlier' : 'later'}` : 'at the same time';
    comparison.replaceChildren(element('p', `First flight is ${cost}${incomplete(first) || incomplete(second) ? ' at the prices shown' : ''} and arrives ${time}.`), columns);
  }
  selectors.forEach(select => select.addEventListener('change', update));
  update();
  return { panel, update };
}

function render(payload, trip) {
  const fees = new Map();
  const candidates = [...payload.matches, ...payload.unverified_baggage];
  const comparison = candidates.length > 1 ? flightComparison(candidates, trip, fees) : null;
  const flightCard = (flight, options) => card(flight, {
    ...options, fees, budget: trip.budget, refreshCompare: () => comparison?.update(),
  });
  if (payload.planner_error) results.append(element('p', payload.reason, 'warning'));
  const [best, ...others] = payload.matches;
  if (best) {
    // The deadline offset may differ from the arrival offset at a DST transition.
    const remaining = Math.floor((new Date(payload.deadline) - new Date(best.final_arrival)) / 60000);
    const timing = remaining > 0 ? `${minutesLabel(remaining)} before your deadline` : 'At your deadline';
    const unspent = trip.budget - best.total;
    const budget = unspent ? `${money(unspent)} under budget` : 'At your budget';
    results.append(flightCard(best, {
      title: 'Recommended itinerary', note: `Lowest total found · ${timing} · ${budget}`, recommended: true,
    }));
    const fastest = others.reduce((selected, current) =>
      !selected || new Date(current.final_arrival) < new Date(selected.final_arrival) ? current : selected, null);
    if (fastest && new Date(fastest.final_arrival) < new Date(best.final_arrival)) {
      const earlier = Math.floor((new Date(best.final_arrival) - new Date(fastest.final_arrival)) / 60000);
      const extra = fastest.total - best.total;
      results.append(flightCard(fastest, {
        title: 'Earlier arrival', note: `${minutesLabel(earlier)} earlier · ${extra ? money(extra) + ' more' : 'Same total'}`,
      }));
    }
    const remainingFlights = others.filter(flight => flight !== fastest || new Date(flight.final_arrival) >= new Date(best.final_arrival));
    if (remainingFlights.length) {
      const details = element('details');
      details.append(element('summary', `View ${remainingFlights.length} more matching ${remainingFlights.length === 1 ? 'flight' : 'flights'}`));
      const list = element('div', undefined, 'more');
      list.append(...remainingFlights.map(flight => flightCard(flight)));
      details.append(list);
      results.append(details);
    }
  } else {
    if (!payload.unverified_baggage.length) {
      results.append(element('h3', payload.fallbacks.length
        ? 'No verified match within both limits' : 'No matching flights found'));
    }
  }
  if (payload.unverified_baggage.length) {
    results.append(element('h3', 'Possible matches · Check bag fees'));
    for (const [index, flight] of payload.unverified_baggage.entries()) {
      results.append(flightCard(flight, {
        note: `Arrives on time · ${money(trip.budget - flight.total)} left for your checked bag`,
        recommended: !best && index === 0,
      }));
    }
  }
  if (!best) {
    for (const fallback of payload.fallbacks) {
      const changes = [];
      if (fallback.extra_cost) changes.push(`Increase your budget by ${money(fallback.extra_cost)}`);
      if (fallback.extra_minutes) changes.push(`Extend your deadline by ${minutesLabel(fallback.extra_minutes)}`);
      const panel = flightCard(fallback.flight, { note: changes.join(' · ') });
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
  }
  if (comparison) results.insertBefore(comparison.panel, results.firstChild);
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

async function search(trip, { fromChat = false } = {}) {
  if (drafting) return;
  activeSearch?.abort();
  const controller = new AbortController();
  activeSearch = controller;
  searching = true;
  submit.disabled = true;
  results.replaceChildren();
  status.textContent = 'Checking your flights. Nearby airports or next-day departures may be included.';
  setActivity('Checking flights…', fromChat);
  results.setAttribute('aria-busy', 'true');
  try {
    const response = await fetch('/api/recover', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(trip),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Check the trip details and try again.');
    if (controller.signal.aborted) return;
    status.textContent = payload.source === 'live'
      ? 'Live fares from Google Flights. Prices can change before booking.'
      : 'Simulated flights · For demonstration';
    render(payload, trip);
    if (fromChat) {
      const best = payload.matches[0];
      chatMessage('assistant', best
        ? `I found a flight that fits your limits. The lowest total I found is ${money(best.total)}. See Results for the details.`
        : payload.unverified_baggage.length
          ? `I found an on-time flight for ${money(payload.unverified_baggage[0].total)} before the bag fee. That leaves ${money(trip.budget - payload.unverified_baggage[0].total)} for your bag. Check the fee with the airline before booking.`
          : 'I didn’t find a match within both limits. See Results for what I found, or tell me what you can change.');
      if (draftState) saveDraft(draftState);
      const view = action('View results', () => {
        document.querySelector('#results-view').click();
        heading.focus();
      }, true);
      view.classList.add('chat-results-action');
      conversation.lastElementChild.append(view);
    }
  } catch (error) {
    if (controller.signal.aborted) return;
    status.textContent = 'Search unavailable';
    results.append(element('p', error.message || 'Try again in a moment.', 'warning'));
    if (fromChat) {
      chatMessage('assistant', `The flight search couldn’t finish. ${error.message || 'Try again in a moment.'}`);
      if (draftState) saveDraft(draftState);
    }
  } finally {
    if (activeSearch === controller) {
      activeSearch = null;
      searching = false;
      submit.disabled = drafting;
      results.setAttribute('aria-busy', 'false');
      if (!drafting) setActivity();
      if (!fromChat) heading.focus();
    }
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
  try { sessionStorage.setItem(storageKey, JSON.stringify({ ...state, transcript })); } catch { /* Storage is optional. */ }
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
    nyc_flexible: Boolean(inputs.destination.value === 'NYC' && draftState?.draft.nyc_flexible),
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

agentForm.addEventListener('submit', event => {
  event.preventDefault();
  sendMessage(agentForm.elements.message.value.trim());
});
agentForm.elements.message.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!agentButton.disabled) agentForm.requestSubmit();
  }
});

async function sendMessage(message, airportChoice, useSuggestedDeparture = false) {
  if (drafting || !modelAvailable || !message) return;
  const source = inputs.source.value;
  activeSearch?.abort();
  results.replaceChildren();
  status.textContent = 'Updating your trip…';
  drafting = true;
  agentButton.disabled = true;
  submit.disabled = true;
  choices.replaceChildren();
  chatMessage('user', message);
  agentForm.elements.message.value = '';
  setActivity('Reading your trip details…');
  let confirmedTrip;
  try {
    const response = await fetch('/api/trip-draft', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message, draft: draftState?.draft || {}, question: draftState?.question,
        airport_choice: airportChoice, use_suggested_departure: useSuggestedDeparture, source,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Check your message and try again.');
    if (inputs.source.value !== source) throw new Error('Flight source changed. Please resend your message.');
    const reply = data.question || 'I’ll look for flights that fit your trip. Results will appear as soon as the search finishes.';
    chatMessage('assistant', reply);
    saveDraft({ ...data, source });
    applyDraft(data.draft);
    showDraft(data);
    agentStatus.textContent = '';
    if (data.question) {
      status.textContent = 'I’ll search after we confirm the remaining trip details.';
    } else {
      confirmedTrip = { ...data.draft, source, use_model: inputs.use_model.checked };
    }
  } catch (error) {
    chatMessage('assistant', error.message);
    agentStatus.textContent = '';
    status.textContent = 'Update your details to continue.';
    if (draftState) {
      showDraft(draftState);
      saveDraft(draftState);
    }
  } finally {
    drafting = false;
    setActivity();
    agentButton.disabled = !modelAvailable;
    submit.disabled = searching;
  }
  if (confirmedTrip) await search(confirmedTrip, { fromChat: true });
}

form.addEventListener('submit', event => {
  event.preventDefault();
  if (drafting || searching) return;
  const draft = readDraft();
  if (!draft.origin || draft.origin === 'NYC' || !draft.destination) {
    status.textContent = 'Choose two airports from the suggestions or enter their three-letter codes.';
    heading.focus();
    return;
  }
  const values = new FormData(form);
  draft.checked_bag = values.has('checked_bag');
  saveDraft({ draft, question: null, source: values.get('source') });
  showDraft(draftState);
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
    catalog.live = [...config.live_airports];
    if (config.live_available) catalog.live.push({ code: 'NYC', label: 'Any NYC airport', zone: 'America/New_York' });
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
          for (const entry of saved.transcript || []) chatMessage(entry.role, entry.message);
          showDraft(saved);
          if (!saved.question && saved.draft.origin && saved.draft.destination) {
            status.textContent = 'Continue chatting or use the form to search this trip again.';
          }
        }
      } catch { /* An expired session does not prevent manual search. */ }
    }
    replaceAirportOptions();
    document.querySelector('#planner-hint').textContent = config.model_available
      ? 'OpenAI can choose a nearby airport or next-day departure when the first search misses. It receives trip constraints and search results.'
      : 'The standard planner is active. Set OPENAI_API_KEY on the server to enable the OpenAI planner.';
    submit.disabled = false;
    if (!transcript.length) chatMessage('assistant', 'Tell me what changed about your flight and when you need to arrive.');
    if (!draftState) status.textContent = 'Enter your trip to see the best available options.';
  } catch {
    status.textContent = 'Could not load search settings. Refresh the page to try again.';
  }
}

initialize();
