# Revision plan and assessment

Baseline: see `evidence/baseline-commit.txt`; The initial stale checkout passed 109 tests; upstream had four newer commits.
The revision was integrated onto upstream b505c16 before final validation.
The discovered checkout was the developer's local macOS clone.
Existing `.gitignore` and `maritime.json` edits were preserved.

## Existing behavior

Implemented: flight normalization, deterministic ranking, SQLite trip/preferences,
cache and quota accounting, booking links, watches, chart/dashboard helpers.
OpenClaw skill and Maritime bootstrap existed. No explicit team configuration,
whole-trip checkpoint/research model or comparative evidence was present.
The Docker image is a Python tool runner, not an OpenClaw/WhatsApp gateway.
`monitor-run` computes notifications; actual transport delivery is separate.

## Revision sequence

1. Clarify product scope and MIT/contribution expectations; retain detailed
   architecture outside the README.
2. Configure the existing OpenClaw harness with one restricted researcher.
   Main agent reviews results and owns state; no custom agent framework.
3. Persist destination preferences, planning checkpoints and phase events.
   Apply remembered airport/cabin defaults without overriding explicit choices.
4. Define bounded recovery, timeouts, source review and stopping rules. Preserve
   upstream removal of transactional booking commands.
5. Compare five scenarios against the saved baseline, add regression tests and
   publish the exact reproducible evidence and unverified deployment steps.

## Limits and next revisions

Checkpoint schema validation checks structure, not truth; parent source review
is still necessary. Tool-action limits are instructions except the child runtime
 timeout and tool allowlist. The main agent has shell access and is a trusted
single-owner service, not a multi-tenant security boundary. Expand to multiple
users only after adding authenticated per-user authorization to all CLI paths.
Hotel/restaurant web research is distinct from inventory/availability APIs.
Notification delivery needs an acknowledged outbox before claiming reliability.

## Live validation follow-up

OpenAI API access was verified using the user's local `.env`. An isolated
OpenClaw gateway exercised actual delegation and exposed instruction/configuration
gaps. Added injected TOOLS.md, scoped agent-to-agent history, an OpenAI coordinator
and researcher model split, explicit trusted test identity, stronger completion
validation, preservation of findings on failure, and opt-in live evaluation tools.
The original failed runs are retained as sanitized behavioral evidence.
