# Travel tool reference (exact syntax)

Run `python3 -m src.cli` from this workspace. Do not guess command names or flags.
Before the first trip action, use `read` on `skills/travel-agent/SKILL.md`.
All JSON can be supplied through stdin (`--json -` or `--request-json -`).

```sh
python3 -m src.cli prefs-get --user USER_ID
python3 -m src.cli prefs-set --user USER_ID --json '{"home_airport":"BOS","dietary_preferences":["vegetarian"],"hotel_max_nightly":180}'
python3 -m src.cli new-trip --user USER_ID --request-json '{"destination":"ORD","outbound_date":{"start":"2027-10-09"},"return_date":{"start":"2027-10-12"}}'
python3 -m src.cli search --trip TRIP_ID
python3 -m src.cli refine --trip TRIP_ID --command cheaper
python3 -m src.cli status --trip TRIP_ID
python3 -m src.cli checkpoint --trip TRIP_ID
python3 -m src.cli checkpoint --trip TRIP_ID --json '{"phase":"researching","next_action":"Await researcher","research":[],"reviewed":false,"unresolved":[]}'
```

Replace USER_ID and TRIP_ID with actual IDs. Dates require `{ "start": "YYYY-MM-DD" }`.
There are no `--phase`, `--key`, `--value`, `--from`, or `--to` CLI flags.
Only claim a preference/checkpoint was saved after that exact command returns `ok: true`.
On syntax errors, read the skill or `python3 -m src.cli COMMAND --help` once; correct
once and then stop with a blocker if it still fails. Never fabricate success.

## Delegation contract

Use `sessions_spawn` with **agentId="travel-researcher"** (never omit it),
`runTimeoutSeconds=120`, `cleanup="keep"`, and a bounded destination brief.
Put the entire brief directly in the `task` string. Omit `attachments` and
`forkContext` entirely: attachments are intentionally disabled, and the child
needs only the minimum travel context, not files or the parent conversation.
Keep timeout and cleanup as top-level tool arguments, not inside the brief.
Use the actual OpenClaw `sessions_spawn` tool, never a native Codex subagent or
a fabricated `agent:travel-researcher:subagent:` session key. A missing tool is
a deployment blocker; only record IDs returned by the real tool.
Include the expected JSON fields in the brief; the child does not inherit your
skill context. Each research item needs category, name, source_url, checked_at
(actual ISO timestamp), rationale, caveats. Top level: research[], unresolved[].

Before spawning, save phase=researching. After accepted, save runId and
childSessionKey in next_action, which must be a STRING, e.g.
`"next_action":"Await runId=abc childSessionKey=agent:travel-researcher:subagent:xyz"`. Do not use `process` to poll a child: that tool
only manages shell processes. Return a pending status and let the completion
announcement resume you. Do not repeatedly call sessions_history.

On completion, review the actual child content against the user's constraints
and source(s). An announcement is not proof of a valid checkpoint. Save phase=ready,
reviewed=true only after verifying the result; otherwise blocked with unresolved.
Retain the child run ID/session key in next_action for traceability.
Relay flight display_text verbatim and clearly label mock fares as demonstration
inventory. Never paraphrase prices/times. A citation alone does not prove that a
hotel or restaurant is available, safe for allergies, or within a future budget.

For any JSON containing names, apostrophes, or prose, use a quoted heredoc:
```sh
python3 -m src.cli checkpoint --trip TRIP_ID --json - <<'TRAVEL_JSON'
{"phase":"blocked","next_action":"Ask to retry research","research":[],"reviewed":false,"unresolved":["Research timed out"]}
TRAVEL_JSON
```
Do not use printf, echo, or nested shell quoting to pass research JSON.
The final response must include flight `display_text` byte-for-byte inside one
fenced text block. Do not insert bold markers, rewrap lines, or remove its footer.
Put explanations outside that block.

A ready checkpoint MUST contain at least one validated research item. Store the
actual child findings, not just a completion message or reviewed=true. If the
result cannot be grounded in a citation, stay blocked. On failure, previously
saved research is retained as historical evidence, not freshly verified facts.
Identity comes from authenticated channel metadata or trusted server-side
context. Never infer a user ID from a filesystem path or OS account name.

## On a child timeout announcement

Before answering the announcement, write this checkpoint using the actual trip ID:
```sh
python3 -m src.cli checkpoint --trip TRIP_ID --json - <<'TRAVEL_JSON'
{"phase":"blocked","next_action":"Ask whether to retry timed-out research; do not respawn automatically","unresolved":["Research child timed out; no new verification"]}
TRAVEL_JSON
```
The tool automatically preserves previous research on researching/blocked updates
with no new findings. Only an explicit planning checkpoint resets research. Confirm `ok: true`, then tell the user the timeout and next action.
Do not leave the checkpoint at researching once a terminal result is known.
