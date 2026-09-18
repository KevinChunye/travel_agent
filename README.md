# travel-agent

A personal flight search and price-monitoring agent you talk to on
WhatsApp. It parses natural-language requests ("Boston to Chicago Oct
10–13, leave after 3pm, under $350"), searches Google Flights live,
ranks options deterministically with explanations, tracks prices toward
a target, charts fare history, and hands you a booking link. **It never
takes payment** — you buy on the airline site, say "booked", and it
saves and monitors the trip.

**Split of responsibilities:** an LLM (via an [OpenClaw](https://openclaw.ai)
skill) handles language and tool orchestration; deterministic Python
handles ranking math, state, persistence, and the SerpAPI quota
(~250 free searches/month, treated as a first-class resource).

## How it works

```
WhatsApp → OpenClaw agent → skills/travel-agent/SKILL.md → python -m src.cli
             │
             ├─ SearchBudgetManager: hard monthly cap, background-protected
             │  reserve, cost estimates for flexible dates, persistent cache
             │  (equivalent searches are free; refinements cost 0 calls)
             ├─ GoogleFlightsProvider: SerpAPI → normalized Offer model
             ├─ ranking/scorer: hard-constraint filter + weighted utility
             │  (price/time/convenience/reliability) with explanations
             ├─ handoff: booking link out, "booked" in → saved Trip + reminders
             ├─ price_watch: adaptive checks (5d/3d/2d/1d, stop <3d before
             │  departure), own PriceObservation history, target alerts
             └─ charts + /travel dashboard; SQLite for all state
```

## Quick start

```bash
pip install -r requirements.txt pytest
python -m pytest                       # 105 tests; SerpAPI fully mocked

export TRAVEL_PROVIDERS=mock           # offline demo (google_flights for live)
python -m src.cli new-trip --user demo --request-json \
  '{"origin":"BOS","destination":"ORD","outbound_date":{"start":"2026-10-10"},"constraints":{"max_price":350}}'
python -m src.cli search  --trip <trip_id>
python -m src.cli refine  --trip <trip_id> --command "under 300"   # free
python -m src.cli select  --trip <trip_id> --option 1
python -m src.cli link    --trip <trip_id>                         # booking URL
python -m src.cli booked  --trip <trip_id>
python -m src.cli track   --trip <trip_id> --option 1 --target 300
python -m src.cli usage · watches · trips · chart · plan · dashboard · monitor-run
```

## Deploy (Maritime + OpenClaw + WhatsApp)

```bash
git clone https://github.com/KevinChunye/travel_agent /data/travel_agent
sh /data/travel_agent/deploy/maritime_setup.sh   # deps, tests, skill install
```

Set env (see `.env.example`): `SERPAPI_API_KEY`, `TRAVEL_PROVIDERS=google_flights`,
`TRAVEL_AGENT_HOME`, `DATABASE_PATH`. Pair WhatsApp via the platform's
Channels UI. Schedule `python3 -m src.cli monitor-run` every few hours
for reminders and price watches. `deploy/Dockerfile` covers generic
Docker hosts.

## Layout

```
skills/travel-agent/SKILL.md  agent playbook (operator of the CLI, never editor)
src/cli.py · bin/travel       deterministic tool surface (JSON out + fallback file)
src/providers/                base ABC · google_flights (active) · mock · duffel*
src/services/                 search_budget, search, handoff, price_watch,
                              charts, dashboard, monitoring, state machine
src/models/ · src/ranking/ · src/storage/   pydantic models, scorer, SQLite repo
tests/                        105 tests: quota, cache, ranking, states, watches
* duffel + trainline/distribusion stubs are dormant legacy, isolated from the
  active workflow (no payments anywhere; no card or passport data collected)
```
