# OpenClaw, Maritime and WhatsApp

The sample config was validated with **OpenClaw 2026.2.14**. It uses `agents.list`;
newer versions may use a different schema. Validate with your installed version
before merging it. References: [subagents](https://docs.openclaw.ai/tools/subagents),
[configuration](https://docs.openclaw.ai/gateway/configuration).

## OpenAI credentials: local and Maritime

The main agent uses `openai/gpt-5.1` with low reasoning; the bounded researcher
uses `openai/gpt-4.1-mini` in the example configuration. These models are supported
by the installed OpenClaw 2026.2.14 catalog.
Change `agents.defaults.model.primary` and `agents.defaults.subagents.model` together
if choosing another OpenAI model supported by your OpenClaw version.

Local setup (keep `.env` ignored by Git):

```sh
python3 -m pip install -r requirements.txt
# Edit .env: OPENAI_API_KEY=your-key
chmod 600 .env
python3 scripts/openclaw_env.py --check
# After merging and validating the OpenClaw configuration:
python3 scripts/openclaw_env.py -- gateway run
```

The launcher reads the repo `.env`, or `--env-file /path/to/.env`, as data without
shell expansion. Nonempty supported values in that file override inherited values
so a stale shell key cannot shadow the selected local key. With no file it uses
process environment variables. `--check` makes one small billable request to
`https://api.openai.com/v1/responses` and prints status/model/usage, never the key.
The check model flag affects only the check, not gateway model configuration.

On **Maritime**, set `OPENAI_API_KEY` in the service's environment/secrets UI and
restart the OpenClaw process. No `.env` file or credential in JSON is needed.
Keep `SERPAPI_API_KEY` for live flights separately. `BRAVE_API_KEY` is optional for
OpenClaw's web_search discovery; web_fetch can read known official links without
it. An OpenAI model key alone does not enable SerpAPI or Brave.

OpenClaw itself normally gives inherited environment precedence over `.env`;
our explicit local launcher reverses that for the supported file values.
[OpenAI API key setup](https://developers.openai.com/api/docs/quickstart) ·
[OpenClaw environment precedence](https://docs.openclaw.ai/help/environment).

## Local or Maritime OpenClaw template

1. Keep the repository at a persistent path (Maritime example `/data/travel_agent`).
   Install `requirements.txt` and pytest; run `python -m pytest -q`.
2. Merge `config/openclaw/openclaw.example.json` into the template's existing
   OpenClaw configuration. Preserve provider credentials, gateway settings,
   channels and existing agents; do not blindly replace the configuration.
   Adjust both workspace paths to the actual checkout. Set the main agent to
   `travel` for the desired channel binding, preserving other bindings.
3. Set `DATABASE_PATH=/data/travel_agent.sqlite3`, `TRAVEL_AGENT_HOME` to the repo,
   `TRAVEL_PROVIDERS=google_flights`, and `SERPAPI_API_KEY` using deployment secrets.
   Configure a supported model and web_search provider. Without a search key,
   web_fetch can inspect known official URLs but cannot discover arbitrary venues.
4. Validate the proposed configuration before activating it:
   `OPENCLAW_CONFIG_PATH=/path/to/proposed.json openclaw config get agents.list`.
   Restart the gateway after validation; check that `travel` can target
   `travel-researcher` and all subagents have only web tools.
5. Pair WhatsApp in your template's channel UI. The example permits paired DMs
   and disables groups. Test with a synthetic request; never publish pairing data.
6. Confirm persistence by restarting the container and running `prefs-get` and
   `checkpoint` against the same database. Persist OpenClaw session state too.

The existing local `maritime.json` selects the OpenClaw template; it is not proof
that a deployment or WhatsApp channel is healthy. `deploy/Dockerfile` is only a
Python tool image. `deploy/maritime_setup.sh` installs dependencies and the skill;
merge the agent configuration separately. Do not run two copies of bootstrap or
monitor jobs concurrently. Check logs after every upgrade.

## Smoke test and evidence

Ask: “Plan a Boston to Chicago trip for October 9–12, 2027. Remember I prefer
vegetarian meals and hotels under USD 180/night. Find flight options, two hotels,
two restaurants and an airport transfer. Delegate the destination research.”

Capture the parent `sessions_spawn` call, child source/tool trace, parent review,
checkpoint output and final response. Start a new conversation and ask it to
resume the same trip; verify memory affects the result. Do not pass `--deliver`
when running local evidence commands. Redact transcripts before publishing.

For a timeout test, use `runTimeoutSeconds=1` on a synthetic research task and
verify a blocked checkpoint plus a partial answer; do not fabricate a timeout.
For live flight failure, use a test deployment with the search key absent and
verify no mock fares or automatic paid retry are substituted.

Scheduled `monitor-run` returns notification payloads: `notifications` (each with
`user_id`, `kind` and `text`) and a combined `display_text`, covering departure and
check-in reminders as well as price alerts. Reminder times are converted from the
origin airport's local time to UTC. Connect and test an acknowledged transport
separately before advertising delivered WhatsApp alerts. Maritime sleeps idle
agents; confirm scheduled runs still fire before relying on them. This revision
does not deploy or send messages to contacts automatically.

The optional dashboard binds to `127.0.0.1` by default because it lists trips and
confirmation codes. Pass `--host 0.0.0.0` only behind authentication.

For opt-in live tests, see [live evaluation](live-evaluation.md).

## Prepare a configuration update safely

On a **dedicated travel gateway**, generate a separate mode-600 proposal:

```sh
python3 scripts/configure_openclaw.py \
  --config /path/to/existing-openclaw.json \
  --workspace /data/travel_agent \
  --output /path/to/travel-proposal.json --route-whatsapp
```

The input must be strict JSON. The command validates against the installed
OpenClaw and does not activate or overwrite any configuration. It preserves
provider/auth settings, existing channel permissions, unrelated agents and
specific WhatsApp routes. Specific routes continue to take precedence over the
new generic travel route. Use `--make-default` only to change the default agent.
Review the proposal privately: it contains copied credentials. Shared changes
apply to every agent on this gateway: low reasoning, one concurrent subagent,
web-only subagent tools, and per-channel-peer DM sessions. Do not apply these
shared policies to an unrelated multi-purpose gateway without review.

Stop the gateway before running `deploy/maritime_setup.sh`. Failed Git updates
now stop setup. Workspace refresh replaces tracked code files without deleting
runtime files, copying `.env`, or copying a live SQLite database. Removed code
files remain for manual review; the update is atomic per file, not per release.
Back up the database with SQLite's backup API before a production upgrade.
Keep the previous configuration privately for rollback, then activate the
validated proposal and restart using the host's normal gateway controls.

`maritime deploy` uploads no local files: it redeploys the template (and your
`maritime.json` instructions). Code reaches the container only through the
setup script's `git clone`/`git pull`, which follows the GitHub **default
branch**. Merge the reviewed branch into the default branch, or run setup with
`TRAVEL_AGENT_BRANCH=<branch>`; setup prints the deployed `code: <branch> @
<commit>` line, so check it. If tracked files were edited inside the container,
setup saves the diff under `data/local-changes-*.patch` and restores the
committed code before pulling. A failed pull or failing tests stop setup, and
the files are re-locked either way. If setup warns that `chattr +i` is
unavailable, the `chmod a-w` fallback does not stop an agent running as root;
the OpenClaw tool allowlist (no write/edit tools) is then the main protection.
Existing secrets in Maritime are not proof that the provider key or configured
base URL is usable. Verify both without logging their values.

## Maritime OpenClaw 2026.7.1 runtime

The deployed Maritime image uses a newer runtime than the local 2026.2.14
baseline. For direct OpenAI API use, merge
`config/openclaw/maritime-runtime.patch.json` into the **existing OpenAI provider**
after preparing the main configuration. Validate before applying it:

```sh
openclaw config patch --file config/openclaw/maritime-runtime.patch.json --dry-run
openclaw config patch --file config/openclaw/maritime-runtime.patch.json
```

This selects provider-scoped `agentRuntime.id: "openclaw"`. Without that pin,
2026.7.1 selected the native Codex backend in the deployment test, whose native
subagent handoff did not produce the configured OpenClaw researcher session.
An attachment-bearing handoff was also rejected; send the brief inline in `task`.
Do not enable attachments or substitute a native child to work around failure.
Validate the patch with the installed version; it is separate from the legacy
example because 2026.2.14 does not share this runtime-selection schema.
Verify actual researcher session and tool traces, not only the parent's narrative.
[OpenClaw runtime configuration](https://docs.openclaw.ai/plugins/sdk-agent-harness/runtime-config).

OpenClaw 2026.7.1 also rejects per-call `runTimeoutSeconds`. This patch sets
`agents.defaults.subagents.runTimeoutSeconds: 120`; calls omit the rejected field.
The isolated 2026.2.14 timeout evaluation uses that older version's per-call field.
Do not run its one-second timeout case against production or assume the two
versions have interchangeable tool schemas.
