# travel-agent

A personal travel-booking agent. You message it in natural language
("Boston to New York October 9 through 11, leave Friday after 4pm,
back Sunday after 2pm — price matters most, but nothing extremely
slow"), it searches transportation providers, ranks options against your
preferences, and — only after showing you the exact refreshed price and
itinerary and getting an explicit yes — books the trip and monitors it.

## Design principle

The **LLM** (via an [OpenClaw](https://openclaw.ai) skill) handles
language: parsing requests, asking for missing details, explaining
options, and orchestrating tools.

**Deterministic Python** handles money and state: constraint filtering,
ranking math, the trip state machine, booking authorization, and
provider transactions. The model cannot bypass these rules through
prompting — the booking service re-validates everything against a
persisted, explicitly confirmed `BookingIntent` at execution time.

## Architecture

```
WhatsApp ──► OpenClaw agent ──► skills/travel-agent/SKILL.md
(iMessage                              │  invokes
 later, via                            ▼
 e.g. Photon)                python -m src.cli <command>   ← deterministic core
                                       │
        ┌──────────────┬───────────────┼────────────────┬──────────────┐
        ▼              ▼               ▼                ▼              ▼
   src/services/  src/ranking/   src/providers/   src/storage/   src/channels/
   search,        scorer         base (ABC)       repository     base (ABC)
   booking,                      duffel │ mock    SQLite now,    whatsapp
   monitoring,                   trainline*       Postgres       (imessage
   state machine                 distribusion*    later          later)
                                 (* Stage 5 stubs)
```

- **Providers** implement one abstract interface — `search`, `refresh`,
  `book`, `cancel`, `get_status` — and return only the normalized
  `Offer` model. Provider payloads never leak out of an adapter.
  [Duffel](https://duffel.com) is the first real provider (flights);
  Trainline/Distribusion stubs show where rail/bus plug in.
- **Ranking** is deterministic: `price_score`, `time_score`,
  `convenience_score`, `reliability_score` in [0, 1], combined with
  normalized user weights into a utility score, with explanations built
  from the actual component values. Hard constraints (nonstop only, max
  price, no red-eye, arrive-by, carry-on required…) filter offers before
  ranking and are never traded off.
- **State machine** (`src/services/state.py`): DRAFT →
  NEEDS_INFORMATION → READY_TO_SEARCH → SEARCHING → OPTIONS_READY →
  OPTION_SELECTED → REPRICING → AWAITING_BOOKING_CONFIRMATION → BOOKING
  → CONFIRMED → MONITORING → COMPLETED, plus OFFER_EXPIRED,
  PRICE_CHANGED, BOOKING_FAILED, PAYMENT_FAILED, TRIP_CHANGED,
  CANCELLED with explicit recovery paths. State is persisted in SQLite;
  conversation history is never the source of truth.
- **Booking authorization**: `reprice` refreshes the selected offer and
  creates an unconfirmed `BookingIntent` (itinerary fingerprint,
  passenger identity hash, approved and maximum price). `book` refuses
  unless the intent was explicitly confirmed, is fresh (15 min), and the
  final live refresh still matches: price within the approved max, same
  itinerary, same baggage, same passenger, not expired. Anything else
  returns `requires_reconfirmation` instead of buying.
- **Monitoring**: confirmed bookings get persistent tasks (departure and
  check-in reminders, recurring status checks). `monitor-run` is a
  single idempotent entrypoint for a **Maritime scheduled trigger** (or
  any cron) — no in-process timers. Users are notified only on relevant
  changes (cancellation, schedule change, delay ≥ 20 min, terminal
  change).
- **Messaging** sits behind `MessagingChannel`
  (`receive_message` / `send_message` / `send_notification`). WhatsApp
  (Cloud API) is the first adapter; iMessage can be added later as a
  sibling adapter without touching travel logic.

## Quick start (Stage 1: mock offers)

```bash
pip install -e ".[dev]"
pytest                      # 58 tests: ranking, state machine, auth rules…

# Conversation flow, as the OpenClaw skill drives it:
python -m src.cli new-trip --user kevin --request-json '{
  "origin": "BOS", "destination": "JFK",
  "outbound_date": {"start": "2026-10-09"},
  "outbound_window": {"earliest": "16:00"},
  "weights": {"price": 55, "time": 25, "convenience": 15, "reliability": 5}}'
python -m src.cli search  --trip <trip_id>
python -m src.cli refine  --trip <trip_id> --command "cheaper"
python -m src.cli select  --trip <trip_id> --option 1
python -m src.cli traveler-add --user kevin --json '{"given_name":"Kevin","family_name":"Wang","born_on":"1995-05-01"}'
python -m src.cli reprice --trip <trip_id>        # shows exact price, creates intent
python -m src.cli confirm --trip <trip_id> --intent <intent_id> --user-confirmed yes
python -m src.cli book    --trip <trip_id> --intent <intent_id>
python -m src.cli monitor-run                     # what the scheduler invokes
```

Set `TRAVEL_PROVIDERS=duffel` and `DUFFEL_API_KEY=duffel_test_…` to
switch from mocked offers to real Duffel search/booking (use a test-mode
key until you mean it).

## Deployment

**OpenClaw**: install `skills/travel-agent/` as a skill in your OpenClaw
workspace and connect the WhatsApp channel in OpenClaw's config; the
skill drives the CLI in this repo. Copy `.env.example` to the
environment (never commit secrets).

**Maritime**: deploy the container and add a scheduled trigger that runs
`python -m src.cli monitor-run` every ~15 minutes. The SQLite file lives
on the `/data` volume.

**Docker (portable to DigitalOcean later)**:

```bash
docker build -t travel-agent .
docker run --env-file .env -v travel_data:/data travel-agent \
  python -m src.cli monitor-run
```

## Security

- No card numbers or CVVs anywhere — payment uses provider-side tokens
  (Duffel balance/token).
- API keys only via environment variables.
- Traveler PII lives in its own `travelers` table, separate from trips
  and conversation state; booking intents store only an identity hash.
- Booking tools enforce authorization in code, independent of the LLM;
  sensitive data is not logged.

## Development stages

1. ✅ NL parsing contract, mocked offers, deterministic ranking,
   WhatsApp/OpenClaw conversation flow, state machine, tests
2. ✅ Duffel flight search adapter (enable with `TRAVEL_PROVIDERS=duffel`)
3. ✅ Offer refreshing + protected booking flow (BookingIntent rules)
4. ✅ Persistent preferences + trip monitoring via scheduled trigger
5. ⬜ Rail (Trainline) and bus (Distribusion) adapters — interface stubs in place
6. ⬜ Calendar integration and learned preferences (selection feedback is
   already recorded)

## Project structure

```
skills/travel-agent/SKILL.md   OpenClaw skill (LLM behavior + tool contract)
src/models/                    Pydantic models (request, offer, booking, prefs)
src/providers/                 base ABC, duffel, mock, trainline*, distribusion*
src/ranking/scorer.py          hard constraints + deterministic utility ranking
src/services/                  search, booking auth, monitoring, state machine
src/storage/repository.py      repository ABC + SQLite implementation
src/channels/                  messaging ABC + WhatsApp adapter
src/cli.py                     deterministic tool surface for the skill
tests/                         ranking, state, auth, expiry, monitoring, storage
```
