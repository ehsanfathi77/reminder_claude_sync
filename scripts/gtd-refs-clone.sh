#!/usr/bin/env bash
# Clone the GTD reference repositories we want to study while designing
# the GTD skill. GitHub is not reachable from the Cowork sandbox, so you need
# to run this locally on your Mac.
#
# What it does:
#   1. Removes the partial/broken clone directory left by the earlier attempt.
#   2. Creates research/gtd-refs/ and clones the five reference repos.
#
# Usage:
#   cd ~/Documents/repos/todo
#   ./scripts/gtd-refs-clone.sh
#
# The first rm may prompt for sudo because git left objects 0444.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REFS_DIR="$REPO_ROOT/research/gtd-refs"

echo "==> Repo root: $REPO_ROOT"

# --- Step 1: clean up previous partial clones --------------------------------
if [[ -d "$REFS_DIR" ]]; then
    echo "==> Removing stale $REFS_DIR (may prompt for sudo — git objects are 0444)"
    if ! rm -rf "$REFS_DIR" 2>/dev/null; then
        echo "    rm -rf failed, retrying with sudo..."
        sudo rm -rf "$REFS_DIR"
    fi
fi

mkdir -p "$REFS_DIR"
cd "$REFS_DIR"

# --- Step 2: clone references ------------------------------------------------
# Each entry: "<local-dir>|<git-url>|<one-line-why>"
REFS=(
    "my-gtd-buddy|https://github.com/realYushi/my-gtd-buddy.git|Closest architectural match: Claude + Apple Reminders GTD buddy"
    "reminders-gtd|https://github.com/petioptrv/reminders-gtd.git|Reminders-native GTD list layout — steal the list naming"
    "cc-gtd|https://github.com/adagradschool/cc-gtd.git|Claude Code-flavoured GTD skill — command surface inspiration"
    "gtd-cc|https://github.com/nikhilmaddirala/gtd-cc.git|Alt Claude-Code GTD take — compare to cc-gtd"
    "gtd-coach-plugin|https://github.com/iamzifei/gtd-coach-plugin.git|Coach-style GTD plugin — review/coaching prompts"
)

for entry in "${REFS[@]}"; do
    local_dir="${entry%%|*}"
    rest="${entry#*|}"
    url="${rest%%|*}"
    why="${rest#*|}"
    echo ""
    echo "==> $local_dir"
    echo "    $why"
    echo "    $url"
    if [[ -d "$local_dir/.git" ]]; then
        echo "    Already cloned — pulling"
        git -C "$local_dir" pull --ff-only
    else
        git clone --depth 50 "$url" "$local_dir"
    fi
done

echo ""
echo "==> Done. Cloned into $REFS_DIR"
ls -la "$REFS_DIR"
