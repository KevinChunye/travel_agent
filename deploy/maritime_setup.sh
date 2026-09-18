#!/bin/sh
# One-shot setup/repair for the travel-agent on a Maritime OpenClaw container.
# Idempotent: safe to re-run after restarts, git updates, or fresh agents.
# Usage (in the agent's Console tab):  sh /data/travel_agent/deploy/maritime_setup.sh
# Fresh container without the repo yet:
#   git clone https://github.com/KevinChunye/travel_agent /data/travel_agent && sh /data/travel_agent/deploy/maritime_setup.sh
set -e

REPO=/data/travel_agent
echo "== travel-agent setup =="

# 1. Code: clone or update.
if [ -d "$REPO/.git" ]; then
    git -C "$REPO" pull --ff-only || echo "(git pull failed; continuing with existing checkout)"
else
    git clone https://github.com/KevinChunye/travel_agent "$REPO"
fi

# 2. pip (Debian images ship python3 without it).
if ! python3 -m pip --version >/dev/null 2>&1; then
    python3 -m ensurepip --upgrade 2>/dev/null \
        || { apt-get update && apt-get install -y python3-pip; }
fi

# 3. Dependencies (PEP 668 flag needed on Debian 12; fall back without it).
python3 -m pip install --break-system-packages -q -r "$REPO/requirements.txt" pytest 2>/dev/null \
    || python3 -m pip install -q -r "$REPO/requirements.txt" pytest

# 4. Verify the deterministic core.
cd "$REPO"
python3 -m pytest -q

# 5. Install the skill as real files in both locations OpenClaw may scan.
for d in /data/.openclaw/workspace/skills /data/.openclaw/skills; do
    mkdir -p "$d"
    rm -rf "$d/travel-agent"
    cp -r "$REPO/skills/travel-agent" "$d/"
    echo "skill installed -> $d/travel-agent"
done

# 6. Keep the agent-reachable workspace copy in sync (used when the agent's
# exec runs with TRAVEL_AGENT_HOME pointing into the workspace). The live
# database under data/ is preserved.
WSCOPY=/data/.openclaw/workspace/travel_agent
if [ -d "$WSCOPY" ]; then
    mkdir -p /tmp/ta-db-backup
    [ -d "$WSCOPY/data" ] && cp -r "$WSCOPY/data" /tmp/ta-db-backup/
    rm -rf "$WSCOPY"
    cp -r "$REPO" "$WSCOPY"
    [ -d /tmp/ta-db-backup/data ] && rm -rf "$WSCOPY/data" \
        && cp -r /tmp/ta-db-backup/data "$WSCOPY/data"
    rm -rf /tmp/ta-db-backup
    echo "workspace copy refreshed -> $WSCOPY (database preserved)"
fi

echo "== DONE. Restart the agent (Sleep, then send a chat message), then ask it: 'list your skills' =="
