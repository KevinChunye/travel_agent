# travel-agent

A personal flight **search and price-monitoring** agent you talk to on
WhatsApp. You message it in natural language ("Boston to Chicago
October 10 through 13, leave Friday after 3, prefer nonstop and under
$350"), it searches Google Flights live, ranks options against your
preferences with explanations, tracks prices on an adaptive schedule,
charts fare history, and hands you a booking link. **It never takes
payment**: you purchase on the airline/Google Flights site, tell the
agent "booked", and it saves and monitors the trip.

## Design principles

- The **LLM** (via an [OpenClaw](https://openclaw.ai) skill) handles
  language: parsing, clarifying, explaining, orchestrating tools.
- **Deterministic Python** handles ranking math, state, persistence —
  and the **SerpAPI search quota, which is treated as a first-class
  resource** (~250 searches/month on the free tier).
- **No financial transactions.** No card numbers, CVVs, or passport
  data are ever collected or stored.

## Architecture

```
WhatsApp ──► OpenClaw agent ──► skills/travel-agent/SKILL.md
                                       │  invokes
                                       ▼
                      python -m src.cli <command>      deterministic core
                                       │
      ┌───────────────┬────────────────┼──────────────────┬─────────────┐
      ▼               ▼                ▼                  ▼             ▼
 SearchBudget    google_flights   ranking/scorer     services/      storage/
 (quota gate,    provider         (hard constraints,  handoff,      SQLite:
  cache,         (SerpAPI →       weighted utility,   price_watch,  trips, offers,
  SearchPlan)    Offer model)     explanations)       charts,       watches, cache,
                                                      dashboard,    api_calls,
                                                      monitoring    observations
```

- **GoogleFlightsProvider** (`src/providers/google_flights.py`): the
  active provider. Searches Google Flights via SerpAPI (one-way, round
  trip, passengers, cabin, stop constraints, departure-time windows) and
  normalizes everything — airline, flight numbers, airports, times,
  duration, layovers, emissions, price, booking link — into the internal
  `Offer` model. Raw SerpAPI payloads never leave the adapter. The
  provider is the **only** code that talks to SerpAPI, and every call
  goes through the budget manager.
- **SearchBudgetManager** (`src/services/search_budget.py`): persists
  monthly usage by category (USER_SEARCH, RETURN_DETAIL, FLEXIBLE_DATE,
  PRICE_MONITOR, BACKGROUND_REFRESH), enforces the monthly limit as a
  hard ceiling, and keeps a reserve (default 25 calls) that background
  monitoring can never consume. `SearchPlan` estimates the cost of
  flexible-date searches before anything runs ("±1 day ≈ 4 extra
  searches") — nothing expands automatically.
- **Persistent cache**: equivalent searches (deterministic fingerprint
  over route/dates/pax/cabin/stops/time-window — *not* weights) are
  served from SQLite for free within a configurable TTL (default 6h).
- **Refinements are free**: `cheaper`, `faster`, `earlier`, `later`,
  `nonstop only`, `under 300`, `more`, weight changes, and option
  selection all rerank the stored offers with **zero** API calls. Only
  an explicit `refresh` or an agreed flexible-date expansion spends
  quota.
- **Booking handoff** (`src/services/handoff.py`):
  `OPTION_SELECTED → BOOKING_LINK_READY → AWAITING_USER_BOOKING`. The
  user gets the Google Flights URL and a clear payment disclaimer; after
  buying they say "booked" and a **BookedTrip** (airline, flight number,
  confirmation code, times — never payment data) is created and
  monitored. Designed so Gmail confirmation-import can populate the same
  model later.
- **Price watches** (`src/services/price_watch.py`): "track 1", "alert
  me under $350". Watches persist full search constraints, check fares
  on an adaptive schedule (>60d out: every 5d · 30–60d: 3d · 14–30d:
  2d · 3–14d: daily · <3d: stop), defer when the monitoring budget is
  exhausted, and record every check as a **PriceObservation** — our own
  fare-history dataset.
- **Charts** (`src/services/charts.py`): matplotlib PNG of a watch's
  observed price history (current price, lowest, target line), rendered
  offline, sized for WhatsApp media. Command: `chart`.
- **Dashboard** (`src/services/dashboard.py`): server-rendered HTML at
  `/travel` — watches, trips, monthly quota. `python -m src.cli
  dashboard --port 8090`.
- **State machine** (`src/services/state.py`): active flow
  `DRAFT → NEEDS_INFORMATION → READY_TO_SEARCH → SEARCHING →
  OPTIONS_READY → OPTION_SELECTED → BOOKING_LINK_READY →
  AWAITING_USER_BOOKING → TRIP_CONFIRMED → MONITORING → COMPLETED`,
  plus `PRICE_WATCH_ACTIVE`, `TRACKING_PAUSED`, `NO_RESULTS`,
  `SEARCH_FAILED`, `SEARCH_QUOTA_REACHED`, `BOOKING_LINK_UNAVAILABLE`.
  Transactional states (BOOKING, PAYMENT_FAILED, …) survive only in an
  isolated legacy path (the dormant Duffel adapter) and are unreachable
  from the active flow.

## Setup

```bash
pip install -r requirements.txt        # pydantic, httpx, matplotlib
pip install pytest && python -m pytest # full suite, all SerpAPI mocked

cp .env.example .env                   # then fill in:
#   SERPAPI_API_KEY=...                # serpapi.com free tier
#   TRAVEL_PROVIDERS=google_flights
#   SERPAPI_MONTHLY_LIMIT=250  SERPAPI_RESERVE=25
#   SEARCH_CACHE_TTL_MINUTES=360  DATABASE_PATH=data/travel_agent.sqlite3
```

Quick tour (mock provider, no key or quota needed —
`TRAVEL_PROVIDERS=mock`):

```bash
python -m src.cli new-trip --user kevin --request-json '{
  "origin":"BOS","destination":"ORD",
  "outbound_date":{"start":"2026-10-10"},
  "return_date":{"start":"2026-10-13"},
  "outbound_window":{"earliest":"15:00"},
  "constraints":{"max_price":350}}'
python -m src.cli search  --trip <trip_id>          # 3 ranked options
python -m src.cli refine  --trip <trip_id> --command "under 300"   # free
python -m src.cli plan    --trip <trip_id> --flex-out 1 --flex-return 1
python -m src.cli select  --trip <trip_id> --option 1
python -m src.cli link    --trip <trip_id>          # booking URL handoff
python -m src.cli booked  --trip <trip_id> --details '{"confirmation_code":"ABC123"}'
python -m src.cli track   --trip <trip_id> --option 1 --target 300
python -m src.cli usage · watches --user kevin · trips --user kevin
python -m src.cli chart   --user kevin              # PNG price history
python -m src.cli monitor-run                       # scheduled trigger entrypoint
python -m src.cli dashboard --port 8090             # /travel overview
```

## Deployment (Maritime + OpenClaw + WhatsApp)

One-shot container setup/repair:

```bash
git clone https://github.com/KevinChunye/travel_agent /data/travel_agent \
  && sh /data/travel_agent/deploy/maritime_setup.sh
```

Env via `maritime env set travel_agent SERPAPI_API_KEY=... TRAVEL_PROVIDERS=google_flights --reload`.
Schedule `python3 -m src.cli monitor-run` (OpenClaw cron or a Maritime
scheduled job) every few hours — it processes due reminders and price
watches; the adaptive schedule and budget reserve decide what actually
calls the API. WhatsApp pairing: Maritime dashboard → Channels →
WhatsApp → Connect.

## Quota discipline (the 250/month contract)

| Action | Cost |
|---|---|
| New search (new route/dates/constraints) | 1 |
| Same search again within cache TTL | 0 (cache) |
| Any refinement, reranking, selection | 0 |
| Explicit `refresh` | 1 |
| Flexible ±N days | quoted by `plan` first, opt-in |
| Price-watch check | 1, PRICE_MONITOR budget, never the reserve |

Default internal budget split (configurable, advisory): 70 user search
· 50 return/detail · 80 monitoring · 25 flexible dates · 25 reserve.
Unused capacity flows freely; the reserve is only for interactive use.

## Testing

`python -m pytest` — 110+ tests: SerpAPI normalization (one-way, round
trip, cabins, stops, layovers, emissions), caching (hit/miss/expiry/
force-refresh), quota counting/reserve/exhaustion, cost estimation,
refinements-without-API (poison-provider proof), state transitions,
booking-link handoff, watch creation, adaptive intervals, observation
persistence, target-price notification, chart PNG generation, dashboard
rendering. **All SerpAPI traffic is mocked; tests never spend quota.**

## Legacy / future

- The Duffel booking adapter and BookingIntent authorization flow remain
  in the codebase (`src/providers/duffel.py`, `src/services/booking.py`)
  but are isolated from the active workflow. Rail/bus provider stubs
  (Trainline, Distribusion) keep the multi-modal interface open.
- Clean extension points, deliberately not implemented yet: Gmail
  booking-confirmation import (populates `BookedTrip`), Google Calendar,
  weather, destination recommendations, iMessage (via the existing
  channel abstraction).
