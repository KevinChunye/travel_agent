# Evaluation and evidence

## Executed results

- Current upstream baseline suite: **94 passed**.
- Revised suite: **103 passed** (94 existing + 9 regression tests).
- OpenClaw **2026.2.14** accepted the example agent configuration through
  `OPENCLAW_CONFIG_PATH=... openclaw config get agents.list`.
- Five deterministic scenarios were run against both the saved Git baseline
  and revised code, each CLI command in a new process and a temporary database.
  Full synthetic commands/results: [comparison.json](evidence/comparison.json).

| Scenario | Baseline | Revised |
|---|---|---|
| Remember vegetarian preference and apply saved home airport | Not met | Pass |
| Resume planning checkpoint after process restart | Command absent | Pass |
| Reject unreviewed ready state, then persist blocked recovery | Command absent | Pass |
| Detect missing flight credentials without invented fares | Pass | Pass |
| Refine stored results with an unusable search provider | Pass | Pass |

This comparison measures the **deterministic tool contract**, not autonomous
LLM decisions. The baseline already stored flight preferences and ranking weights;
the memory scenario specifically checks newly supported dining preferences and
automatic home-airport application. Synthetic child output is supplied by the
evaluation driver; it is not a captured subagent response.

## Reproduce

```sh
pip install -r requirements.txt pytest
python -m pytest -q
python scripts/evaluate.py
OPENCLAW_CONFIG_PATH="$PWD/config/openclaw/openclaw.example.json" openclaw config get agents.list
```

The evaluator extracts the recorded baseline commit via `git archive`; use a full
Git clone (or fetch that commit first). No API keys or network are needed for
the five scenarios. Fixtures use future synthetic travel dates and mock flights.
Each evaluator run replaces the comparison file with newly generated trip IDs.

## Observed OpenAI/OpenClaw runs

The saved local `.env` key successfully authenticated to the OpenAI Responses
API. Live harness tests ran in an isolated local gateway using mock flights and
an official CTA web page. No WhatsApp channel was configured or messages delivered.
See [sanitized tool/session evidence](evidence/openclaw-live.json) and
[reproduction instructions](live-evaluation.md).

The initial configuration used GPT-4.1 mini for both roles and failed: invented
CLI flags, an omitted delegation target, and a claimed checkpoint that was not
saved. Adding TOOLS.md exposed a disabled cross-agent history policy. The final
configuration enables scoped cross-agent access, uses GPT-5.1/low for the parent
and GPT-4.1 mini for the researcher, supplies trusted identity in test RPC context,
and requires cited findings before a ready checkpoint. This is a comparison of
**whole configurations**, not an isolated model benchmark.

| Live scenario | Observed behavior |
|---|---|
| Multi-step trip + delegation | Real CLI flight search; one named researcher fetched CTA; parent independently fetched the source; four cited findings persisted; unresolved facts correctly kept the plan blocked |
| Gateway restart + new session | Saved vegetarian preference and hotel budget retrieved; new JFK trip inherited BOS without specifying origin; no repeated search or delegation |
| Local refinement | One refine call, no search or web calls, exact display_text returned |
| Missing provider credential | One deliberately failing search, SEARCH_FAILED detected, blocked checkpoint saved with all four previous findings retained |
| Child timeout | Actual one-second timeout; parent saved blocked, retained four prior findings, and did not respawn |

Failures remain in the evidence. An intermediate memory run lacked trusted
identity and guessed an OS username; the corrected run receives user_id via
server-side context. An intermediate parent saved ready with no findings; the
new deterministic validator rejects that. A later timeout announcement gave a
correct explanation without changing state; the background transition instruction
was strengthened and retested. That rerun exposed a second issue: a new researching
checkpoint could clear older findings. The store now retains findings during all
non-planning updates, with a regression test and an actual timeout rerun. The final
retention test restored four earlier synthetic findings as explicit fixture setup;
the retained state is not presented as a new research result. Do not grade success from narrative or exit code.

## Reflection and remaining limitations

The tests demonstrate real tool use, delegation, durable memory, source review,
and recovery handling. They also show why prompt instructions alone are
insufficient: checkpoint validation and observed tool traces caught false claims.
A blocked trip with explicit missing facts is preferable to claiming unverifiable
future fares or availability. One successful run is not a reliability rate.

Action-count limits and explicit delegation targeting remain instructional;
OpenClaw also permits same-agent children, although global subagent policy limits
their tools. The main agent has shell access and assumes a trusted single owner.
Hotel nightly preferences currently store an amount without currency, so the
agent must ask for currency when it is absent from the current request.
Maritime deployment, WhatsApp pairing/delivery, real flight inventory and hotel
availability remain unverified. An acknowledged notification outbox and broader
repeated live evaluations are future work.
