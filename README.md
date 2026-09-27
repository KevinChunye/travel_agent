# travel-agent

An open-source personal travel planner powered by **OpenClaw**, with deterministic
Python flight tools and a WhatsApp interface through OpenClaw. **MIT licensed.**

Search and rank flights, refine options without another search, remember travel
preferences, and resume planning from SQLite checkpoints. A bounded research
subagent can find hotels, restaurants and local transport using cited web sources.
Purchases happen on airline/travel websites through booking links.

**Status:** prototype. Flight tools have offline tests. Destination research needs
an OpenClaw model and web tools; it does not verify room availability or make
reservations. Maritime/WhatsApp deployment requires configuration and pairing.
See [evaluation evidence](docs/evaluation.md) for what has actually been tested.

## Quick start

Python 3.11+:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
python -m pytest -q
export TRAVEL_PROVIDERS=mock
export DATABASE_PATH=data/travel_agent.sqlite3
python -m src.cli prefs-set --user demo --json '{"home_airport":"BOS"}'
python -m src.cli new-trip --user demo --request-json '{"destination":"JFK","outbound_date":{"start":"2027-10-09"}}'
# Use the returned trip_id:
python -m src.cli search --trip <trip_id>
python -m src.cli refine --trip <trip_id> --command cheaper
```

Mock fares are demonstration data, labelled `DEMO DATA` in every result, and are
used only when `TRAVEL_PROVIDERS=mock` is set explicitly; with it unset the CLI
uses live Google Flights and fails without a key. For live flight search, set
`TRAVEL_PROVIDERS=google_flights` and `SERPAPI_API_KEY` in your environment.
See [.env.example](.env.example); copying it to `.env` does not load it automatically.
Search quota and the protected monitoring reserve are configurable.

## Run the agent

Set `OPENAI_API_KEY` in a local `.env` or Maritime environment variables.
Run `python3 scripts/openclaw_env.py --check` to verify access.
Follow [OpenClaw + Maritime setup](docs/deployment.md). The configuration uses a
main travel agent and one restricted destination researcher. Existing OpenClaw
model credentials and WhatsApp pairing remain deployment-specific.

The loop is **load preferences → plan/checkpoint → call tools/delegate → review
sources and constraints → persist → reply or report a blocker**. Tool policy
restricts the child to web research; the main agent owns persistent state.

## Project guide

- [Revision plan and current assessment](docs/revision-plan.md)
- [Evaluation and reproduction](docs/evaluation.md)
- [Tool and architecture reference](docs/architecture.md)
- [Agent instructions](skills/travel-agent/SKILL.md)
- [Contributing](CONTRIBUTING.md) · [MIT license](LICENSE)

Next: real hotel availability adapters, source-grounded restaurant matching,
and reliable notification delivery. Rail/bus support is not implemented.
