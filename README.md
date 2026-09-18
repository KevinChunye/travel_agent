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

## Harness walkthrough

What happens when you send a WhatsApp message to the deployed agent:

1. **WhatsApp → OpenClaw gateway** (hosted on Maritime): the message
   lands in the agent's session.
2. **Skill loads**: `skills/travel-agent/SKILL.md` is the playbook the
   model follows — which command to run for which intent, and the rule
   that it operates the CLI but never edits code.
3. **Model → CLI**: the model turns your sentence into one structured
   command, e.g. `python3 -m src.cli search --trip <id>`, run via exec
   in the repo directory (`TRAVEL_AGENT_HOME`).
4. **Deterministic core**: the CLI checks the search budget and cache,
   calls SerpAPI only when necessary, filters/ranks offers, advances
   the trip state machine, and persists everything to SQLite.
5. **JSON back → reply**: the command prints one JSON object (also
   mirrored to `data/last_response.json` as a fallback); the model
   relays the numbers verbatim — it never invents fares or rankings.
6. **Background**: a scheduled `monitor-run` fires price watches and
   trip reminders on an adaptive cadence, protected by a quota reserve.

## Architecture

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
python -m pytest                       # 94 tests; SerpAPI fully mocked

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

## Skill integrity

The agent must not be able to rewrite its own playbook or code. Two
layers enforce that:

- **Policy**: `SKILL.md` and `AGENTS.md` state the agent is the
  operator of this tool, never its developer; code changes arrive only
  via `git pull`.
- **Enforcement**: `deploy/maritime_setup.sh` marks every code and
  skill file immutable (`chattr +i`, falling back to `chmod a-w`) after
  installing, so the agent's write tools fail on them; only `data/`
  stays writable. The setup script unlocks, updates from git, and
  re-locks — re-run it to ship changes.

## Layout

```
skills/travel-agent/SKILL.md  agent playbook (operator of the CLI, never editor)
src/cli.py · bin/travel       deterministic tool surface (JSON out + fallback file)
src/providers/                base ABC · google_flights (live, search-only) · mock
src/services/                 search_budget, search, handoff, price_watch,
                              charts, dashboard, monitoring, state machine
src/models/ · src/ranking/ · src/storage/   pydantic models, scorer, SQLite repo
tests/                        94 tests: quota, cache, ranking, states, watches
deploy/                       maritime_setup.sh (install + lock) · Dockerfile
```

No payment flow exists anywhere: no card, CVV, or passport data is
collected, and the transactional trip states are legacy-only and
unreachable (test-asserted).
