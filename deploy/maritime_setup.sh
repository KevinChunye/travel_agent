#!/bin/sh
# One-shot setup/repair for the travel-agent on a Maritime OpenClaw container.
# Stop the gateway first. Re-run after git updates or on fresh agents.
# Usage (in the agent's Console tab):  sh /data/travel_agent/deploy/maritime_setup.sh
# Fresh container without the repo yet:
#   git clone https://github.com/KevinChunye/travel_agent /data/travel_agent && sh /data/travel_agent/deploy/maritime_setup.sh
#
# Code reaches the container ONLY through this script's git clone/pull:
# `maritime deploy` on your Mac uploads no files. The GitHub default branch
# is used unless TRAVEL_AGENT_BRANCH names another one, e.g.
#   TRAVEL_AGENT_BRANCH=my-branch sh /data/travel_agent/deploy/maritime_setup.sh
# The deployed branch and commit are printed below; check them.
#
# Optional overrides: TRAVEL_AGENT_REPO (checkout path), TRAVEL_AGENT_REPO_URL,
# OPENCLAW_STATE_DIR (default /data/.openclaw).
set -e

REPO="${TRAVEL_AGENT_REPO:-/data/travel_agent}"
REPO_URL="${TRAVEL_AGENT_REPO_URL:-https://github.com/KevinChunye/travel_agent}"
BRANCH="${TRAVEL_AGENT_BRANCH:-}"
OC="${OPENCLAW_STATE_DIR:-/data/.openclaw}"
WSCOPY="$OC/workspace/travel_agent"
echo "== travel-agent setup =="

# Write protection: the agent operates this tool, it never edits it.
# Code and skill files are made immutable with chattr +i, which even a root
# process cannot write through. Where chattr is unavailable (it needs
# CAP_LINUX_IMMUTABLE) the fallback is chmod a-w, which stops a non-root
# agent but NOT a root one. data/, .git/ and __pycache__/ stay writable.
# This script is the only path that unlocks, updates via git, and re-locks;
# the EXIT trap re-locks even when a step fails.
lock_tree() {
    [ -d "$1" ] || return 0
    find "$1" \( -name data -o -name .git -o -name __pycache__ \) -prune \
        -o -type f -print | while read -r f; do
        chattr +i "$f" 2>/dev/null || chmod a-w "$f" 2>/dev/null || true
    done
}
unlock_tree() {
    [ -d "$1" ] || return 0
    find "$1" \( -name data -o -name .git \) -prune -o -type f \
        -exec chattr -i {} \; 2>/dev/null
    chmod -R u+w "$1" 2>/dev/null || true
}
chattr_works() {
    [ -d "$1" ] || return 1
    probe=$(mktemp "$1/.lockprobe.XXXXXX" 2>/dev/null) || return 1
    if chattr +i "$probe" 2>/dev/null; then
        chattr -i "$probe"; rm -f "$probe"; return 0
    fi
    rm -f "$probe"; return 1
}
relock() {
    status=$?
    set +e
    trap - EXIT
    for p in "$REPO" "$WSCOPY" "$OC/workspace/skills/travel-agent" \
             "$OC/skills/travel-agent"; do
        lock_tree "$p" && [ -d "$p" ] && echo "locked (read-only) -> $p"
    done
    if ! chattr_works "$REPO"; then
        echo "WARNING: chattr +i is unavailable here; files are only chmod a-w,"
        echo "         which does not stop an agent running as root."
    fi
    if [ "$status" -ne 0 ]; then
        echo "== SETUP FAILED (exit $status). Code and skill files were re-locked. =="
    fi
    exit "$status"
}
trap relock EXIT
trap 'exit 130' INT TERM

# 0. Unlock everything this script needs to update or replace.
for p in "$REPO" "$WSCOPY" "$OC/workspace/skills/travel-agent" \
         "$OC/skills/travel-agent"; do
    unlock_tree "$p"
done

# 1. Code: clone or update. Edits made inside the container are saved as a
# patch under data/ and reverted: code changes arrive only through git.
if [ -d "$REPO/.git" ]; then
    if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
        mkdir -p "$REPO/data"
        patch="$REPO/data/local-changes-$(date -u +%Y%m%dT%H%M%SZ).patch"
        git -C "$REPO" diff HEAD > "$patch"
        echo "WARNING: tracked files were modified in the container."
        echo "         Saved the diff to $patch and restored the committed code."
        git -C "$REPO" reset -q --hard HEAD
    fi
    if [ -n "$BRANCH" ]; then
        git -C "$REPO" fetch -q origin "$BRANCH"
        git -C "$REPO" checkout -q "$BRANCH" 2>/dev/null \
            || git -C "$REPO" checkout -q -b "$BRANCH" --track "origin/$BRANCH"
    fi
    git -C "$REPO" pull --ff-only
elif [ -n "$BRANCH" ]; then
    git clone -b "$BRANCH" "$REPO_URL" "$REPO"
else
    git clone "$REPO_URL" "$REPO"
fi
echo "code: $(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" log -1 --format='%h %cd %s' --date=short)"

# 2. pip (Debian images ship python3 without it).
if ! python3 -m pip --version >/dev/null 2>&1; then
    python3 -m ensurepip --upgrade 2>/dev/null \
        || { apt-get update && apt-get install -y python3-pip; }
fi

# 3. Dependencies (PEP 668 flag needed on Debian 12; fall back without it).
python3 -m pip install --break-system-packages -q -r "$REPO/requirements.txt" pytest 2>/dev/null \
    || python3 -m pip install -q -r "$REPO/requirements.txt" pytest

# 4. Verify the deterministic core (a failure stops setup, still re-locked).
cd "$REPO"
python3 -m pytest -q -p no:cacheprovider

# 5. Install the skill as real files in both locations OpenClaw may scan.
for d in "$OC/workspace/skills" "$OC/skills"; do
    mkdir -p "$d"
    rm -rf "$d/travel-agent"
    cp -r "$REPO/skills/travel-agent" "$d/"
    echo "skill installed -> $d/travel-agent"
done

# 6. Update only tracked code. Never delete the workspace or copy a live DB.
# Stop the gateway before running setup; this is not a release-wide atomic swap.
if [ -d "$WSCOPY" ]; then
    python3 "$REPO/scripts/sync_workspace.py" "$REPO" "$WSCOPY"
fi

# 7. Re-locking happens in the EXIT trap.
echo "== DONE. Restart the agent (Sleep, then send a chat message), then ask it: 'list your skills' =="
