#!/usr/bin/env bash
# ==============================================================
# config/agents/cursor/setup.sh — install + check the cursor
# agent.
#
# Invoked by Hearth-Core's FastAPI lifespan (server.py) when
# AGENT_NAME=cursor and HEARTH_SKIP_BOOT_INSTALL is not 1.
# In a space the CLI should be preloaded in the image, so this is
# a no-op there; on a laptop it installs the CLI once with
# Cursor's own installer. Every step is idempotent.
#
# Auth (jobs and chat turns fail without one of these):
#     a native login (`cursor-agent login`, or Connect Cursor in the web app)
#     or CURSOR_API_KEY
#
# Shape mirrors config/agents/claude_code/setup.sh.
# ==============================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/manifest.json"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[cursor]${NC} $*"; }
warn() { echo -e "${YELLOW}[cursor]${NC} $*"; }
fail() { echo -e "${RED}[cursor]${NC} $*"; }

INSTALL_URL="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['runtime_setup']['install_script'])" "$MANIFEST" 2>/dev/null || echo "https://cursor.com/install")"
CLI="${CURSOR_CLI_PATH:-cursor-agent}"
export PATH="$HOME/.local/bin:$PATH"

# ── 1. CLI ────────────────────────────────────────────────────
if command -v "$CLI" >/dev/null 2>&1; then
  ok "cursor-agent present: $("$CLI" --version 2>/dev/null | head -1)"
elif command -v curl >/dev/null 2>&1; then
  warn "cursor-agent missing; installing from $INSTALL_URL"
  if curl -fsSL "$INSTALL_URL" | bash >/dev/null 2>&1 && command -v cursor-agent >/dev/null 2>&1; then
    ok "installed cursor-agent into ~/.local/bin"
  else
    fail "the Cursor installer failed"
    exit 1
  fi
else
  fail "cursor-agent missing and curl unavailable; preload the CLI in the space image"
  exit 1
fi

# ── 2. Auth ───────────────────────────────────────────────────
if [[ -n "${CURSOR_API_KEY:-}" ]]; then
  ok "CURSOR_API_KEY present"
elif "$CLI" status 2>/dev/null | grep -qi "logged in as"; then
  ok "native Cursor login present"
else
  warn "not signed in: run \`cursor-agent login\`, use Connect Cursor in the web app, or set CURSOR_API_KEY"
fi

# ── 3. Home dir ───────────────────────────────────────────────
mkdir -p "$HOME/.cursor"
exit 0
