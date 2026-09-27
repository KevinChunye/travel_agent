# Reproduce the live OpenClaw evaluation

These tests call OpenAI and incur usage. They use mock flight inventory, a
synthetic user, a separate SQLite database, a loopback-only gateway, and no
messaging channels. They do not book travel or deliver WhatsApp messages.
Tested harness: OpenClaw 2026.2.14. Main: openai/gpt-5.1 (low reasoning).
Researcher: openai/gpt-4.1-mini. Model aliases can change; inspect returned metadata.

## Prepare

```sh
python3 -m pip install -r requirements.txt
python3 scripts/openclaw_env.py --check
python3 scripts/live_eval.py prepare --port 18973
```

Use a free port; never stop another gateway to free it. Preparation refuses to
overwrite an existing run. Use `--directory data/another-eval` for a separate run.
Raw results and OpenClaw transcripts remain under the ignored `data/live-eval/`.
Only code/instructions are copied; `.env` and production databases are not copied.

In two terminals, set the same temporary `OPENCLAW_GATEWAY_TOKEN` (a random local
test token you choose; **not** your OpenAI key). Then, in terminal 1:

```sh
python3 scripts/live_eval.py gateway
```

This explicitly enables unattended shell execution only in the trusted synthetic
evaluation workspace. That setting is not in the production example config.
The launcher reads the local `.env`; on a host without one, it uses environment
variables. `--env-file /path/to/.env` explicitly selects a file.

## Five cases

In terminal 2:

```sh
python3 scripts/live_eval.py case team
```

Wait for the research completion announcement. Inspect the raw JSON and the
parent/child session logs under `data/live-eval/state/agents/*/sessions/`.
Get the returned trip ID. Stop **only this evaluation gateway** with Ctrl+C in
terminal 1, then run `python3 scripts/live_eval.py gateway` again with the same
directory and token. In terminal 2:

```sh
python3 scripts/live_eval.py case memory --trip TRIP_ID
python3 scripts/live_eval.py case refine --trip TRIP_ID
python3 scripts/live_eval.py case provider-failure --trip TRIP_ID
python3 scripts/live_eval.py case timeout --trip TRIP_ID
```

Each case uses a fresh session key and a trusted server-side synthetic identity.
Fixtures are in `evals/live_cases.json`. The timeout case intentionally overrides
the normal 120-second child limit with one second. Wait for its completion event
before grading; the initial pending response is not the final state.

## Grade observed behavior

1. **Team:** parent calls the real CLI, spawns exactly one `travel-researcher`,
   child fetches the supplied official source, parent checks its result and saves
   cited research. Missing facts should produce a blocked checkpoint, not a
   fabricated ready state. Record whether displayed flight text is exact.
2. **Memory:** after gateway restart, a new session retrieves the saved food
   preference and hotel budget, reads the old checkpoint, and creates a new trip
   with the saved origin despite omitting origin from its request. No new search.
3. **Refine:** uses stored offers, no search/spawn/web calls, and relays display_text.
4. **Provider failure:** one missing-key search yields SEARCH_FAILED; no retries
   or mock substitution; checkpoint becomes blocked with an actionable next step.
5. **Timeout:** actual child registry outcome is timeout; parent persists blocked
   without another spawn, retaining prior research as historical findings.

A zero CLI exit status is NOT evidence of success: examine tool errors, SQLite
state, source review, and child completion. Preserve failed attempts alongside
fixes. Publish only sanitized evidence; raw fetched pages, reasoning metadata,
local paths, credentials and personal travel histories do not belong in Git.

OpenClaw 2026.2.14 permits same-agent child sessions as well as configured targets.
The `agentId=travel-researcher` requirement is instructional; the global subagent
web-only tool policy still constrains any child. The instruction-based action
budget is also not a hard counter. This prototype assumes a trusted single owner.

## Export minimal evidence

After all completion announcements have settled:

```sh
python3 scripts/summarize_live_eval.py data/live-eval --output data/live-eval/summary.json
```

This extracts operation names, child outcomes, checkpoint counts and selected
synthetic preference/trip fields. It omits page bodies, prompts, final response
text, command prose and reasoning metadata. Review the summary before publishing;
it is not a general-purpose redactor for real-user transcripts.
