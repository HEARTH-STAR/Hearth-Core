#!/usr/bin/env bash
# ==============================================================
# config/agents/antigravity/setup.sh — check the antigravity agent.
#
# Invoked by Hearth-Core's FastAPI lifespan (server.py) when
# AGENT_NAME=antigravity and HEARTH_SKIP_BOOT_INSTALL is not 1.
# The agy CLI comes from Google's installer
# (https://antigravity.google/download); this script does not
# install it, only reports what is there. Every step is idempotent.
#
# Auth: agy reads a self-refreshing Google OAuth token file. There is
# no `agy login`: sign in from the web app (Connect Antigravity) or
# run `agy` once in a terminal.
#
# Shape mirrors xo-space config/agents/antigravity/setup.sh.
# ==============================================================

set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[antigravity]${NC} $*"; }
warn() { echo -e "${YELLOW}[antigravity]${NC} $*"; }
fail() { echo -e "${RED}[antigravity]${NC} $*"; }

export PATH="$HOME/.local/bin:$PATH"
CLI="${AGY_CLI_PATH:-agy}"
AGY_HOME="$HOME/.gemini/antigravity-cli"

# ── 1. CLI ────────────────────────────────────────────────────
if command -v "$CLI" >/dev/null 2>&1; then
  ok "agy present: $("$CLI" --version 2>/dev/null | head -1)"
else
  fail "agy missing; install it from https://antigravity.google/download or preload it in the space image"
  exit 1
fi

# ── 2. Auth ───────────────────────────────────────────────────
if [[ -f "$AGY_HOME/antigravity-oauth-token" ]]; then
  ok "Antigravity login present"
else
  warn "not signed in: use Connect Antigravity in the web app, or run \`agy\` once in a terminal"
fi

# ── 3. Home dirs ──────────────────────────────────────────────
mkdir -p "$AGY_HOME" "$HOME/.gemini/config"
exit 0
