#!/usr/bin/env bash
# ==============================================================
# install.sh — set up and run Hearth-Core natively.
#
# Prepares a virtual environment with uv and runs `server.py` in the
# foreground. git and a network connection are the only prerequisites;
# uv (and through it, Python) is installed for you if missing.
#
# Two ways to use it, chosen automatically:
#
#   From a checkout — uses that working tree in place and never runs a
#   git command against it, so your local edits stay yours:
#       cd Hearth-Core && ./install.sh
#
#   Standalone — clones HEARTH_SOURCE_REPO into ./<repo-name> and runs
#   from there. Re-running fast-forwards that checkout:
#       HEARTH_SOURCE_REPO=<git url> bash install.sh
#
# Options:
#   --no-start   set everything up, then exit instead of starting the server
#   -h, --help   show this help
#
# The server runs in the foreground: its output appends to
# $HEARTH_STATE_DIR/logs/hearth-core.log, Ctrl-C stops it, and re-running
# updates dependencies and restarts. There is no daemon mode: inside a
# space the image's process supervisor runs Hearth-Core, not this script.
#
# Every value below can be overridden from the caller's environment,
# e.g.  PORT=5020 ./install.sh
#
# Must run under bash (not sh): it uses BASH_SOURCE and pipefail.
# Shape mirrors xo-space install.sh.
# ==============================================================

set -Eeuo pipefail

SOURCE_REPO="${HEARTH_SOURCE_REPO:-}"
SOURCE_REF="${HEARTH_SOURCE_REF:-main}"
LAUNCH_DIR="$PWD"
PYTHON_VERSION="${HEARTH_PYTHON_VERSION:-3.12}"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

START_SERVER=1

# Set by resolve_repo_dir, once we know whether we are running from a
# checkout or have to create one.
REPO_DIR=""
VENV_DIR=""
VENV_PYTHON=""
MANAGED_CHECKOUT=0

fail() {
    printf '\nHearth-Core: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "$2"
}

usage() {
    sed -n '3,/^# =====/p' "${BASH_SOURCE[0]:-$0}" | sed '$d; s/^# \{0,1\}//'
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-start) START_SERVER=0 ;;
            -h|--help) usage; exit 0 ;;
            *) fail "Unknown option: $1 (see --help)" ;;
        esac
        shift
    done
}

# ==============================================================
# Where the code is. A checkout is recognised by server.py and
# config/agents/ next to this script; anything else is a standalone
# run, which needs HEARTH_SOURCE_REPO to know what to clone.
# ==============================================================
resolve_repo_dir() {
    local script_dir=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi

    if [ -n "$script_dir" ] && [ -f "${script_dir}/server.py" ] && [ -d "${script_dir}/config/agents" ]; then
        REPO_DIR="$script_dir"
        return
    fi

    [ -n "$SOURCE_REPO" ] || fail \
        "Run this script from a Hearth-Core checkout, or set HEARTH_SOURCE_REPO=<git url> to clone one."
    local repo_name="${SOURCE_REPO##*/}"
    repo_name="${repo_name%.git}"
    REPO_DIR="${HEARTH_APP_DIR:-${LAUNCH_DIR}/${repo_name}}"
    MANAGED_CHECKOUT=1
}

fetch_repo() {
    if [ -d "${REPO_DIR}/.git" ]; then
        if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
            printf 'Local changes in %s; not updating it.\n' "$REPO_DIR"
            return
        fi
        printf 'Updating %s...\n' "$REPO_DIR"
        git -C "$REPO_DIR" fetch --quiet origin "$SOURCE_REF" ||
            fail "Could not fetch ${SOURCE_REF} from ${SOURCE_REPO}."
        git -C "$REPO_DIR" merge --quiet --ff-only FETCH_HEAD ||
            fail "${REPO_DIR} has diverged from ${SOURCE_REF}; update it by hand."
        return
    fi
    [ ! -e "$REPO_DIR" ] || fail "${REPO_DIR} exists and is not a git checkout. Move it, or set HEARTH_APP_DIR."
    printf 'Cloning %s into %s...\n' "$SOURCE_REPO" "$REPO_DIR"
    git clone --quiet --branch "$SOURCE_REF" -- "$SOURCE_REPO" "$REPO_DIR" ||
        fail "Could not clone ${SOURCE_REPO}."
}

# ==============================================================
# uv — the only tool this script installs for you. It installs into
# ~/.local/bin without sudo and brings its own managed CPython, so
# the host needs no system Python.
# ==============================================================
ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return
    fi
    if [ -x "${HOME}/.local/bin/uv" ]; then
        PATH="${HOME}/.local/bin:${PATH}"
        export PATH
        return
    fi

    require_command curl \
        "curl is required to install uv. Install uv yourself instead: https://docs.astral.sh/uv/getting-started/installation/"
    printf 'Installing uv...\n'
    curl -LsSf "$UV_INSTALL_URL" | sh >/dev/null ||
        fail "Could not install uv. Install it manually: https://docs.astral.sh/uv/getting-started/installation/"
    PATH="${HOME}/.local/bin:${PATH}"
    export PATH
    command -v uv >/dev/null 2>&1 ||
        fail "uv was installed but is not on PATH. Add ~/.local/bin to PATH and run this script again."
}

# ==============================================================
# Python environment at venv/ (the path CLAUDE.md and
# hearth-core.sh document). Created only when absent, so repeat runs
# skip straight to the dependency sync. To rebuild: rm -rf venv
# ==============================================================
resolve_venv_python() {
    VENV_DIR="${REPO_DIR}/venv"
    if [ -x "${VENV_DIR}/Scripts/python.exe" ]; then
        VENV_PYTHON="${VENV_DIR}/Scripts/python.exe"  # Windows (Git Bash) layout
    else
        VENV_PYTHON="${VENV_DIR}/bin/python"
    fi
}

sync_dependencies() {
    resolve_venv_python
    if [ ! -x "$VENV_PYTHON" ]; then
        printf 'Creating the Python %s environment in %s...\n' "$PYTHON_VERSION" "$VENV_DIR"
        uv venv --quiet "$VENV_DIR" --python "$PYTHON_VERSION" ||
            fail "Could not create the virtual environment at ${VENV_DIR}."
        resolve_venv_python
    fi

    local requirements="${REPO_DIR}/requirements.txt"
    printf 'Installing dependencies from %s...\n' "${requirements##*/}"
    uv pip install --quiet --python "$VENV_PYTHON" --requirement "$requirements" ||
        fail "Could not install the dependencies in ${requirements##*/}."
}

# ==============================================================
# Runtime tools. The server boots without them, but a HEARTH job
# needs all three: gh holds the GitHub credential and opens PRs, git
# mirrors and pushes, claude is the coding intelligence.
# ==============================================================
report_tool() {
    local binary="$1"
    local purpose="$2"
    if command -v "$binary" >/dev/null 2>&1; then
        printf '  present  %-7s %s\n' "$binary" "$purpose"
    else
        printf '  MISSING  %-7s %s\n' "$binary" "$purpose"
    fi
}

check_tools() {
    printf '\nRuntime tools:\n'
    report_tool git    "repo mirrors, credential-free sandbox trees, pushes"
    report_tool gh     "holds the GitHub credential (PAT or device login), PRs, statuses"
    report_tool claude "Claude Code CLI, the coding intelligence"
    report_tool node   "needed to install the claude CLI (npm i -g @anthropic-ai/claude-code)"
    printf '  The server starts without them; anything MISSING disables the feature next to it.\n'
    printf '  To install the claude CLI on start, set HEARTH_SKIP_BOOT_INSTALL=0 in .env.\n'
}

# ==============================================================
# .env — read without overriding what the caller exported, and
# written once (from .env.example) on the first run. Never rewritten.
# ==============================================================
load_env_file() {
    local env_file="${REPO_DIR}/.env"
    [ -f "$env_file" ] || return 0

    local line key value
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        case "$line" in ''|'#'*) continue ;; esac
        case "$line" in *=*) ;; *) continue ;; esac
        key="${line%%=*}"
        key="${key#export }"
        key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        value="${line#*=}"
        value="${value%%[[:space:]]#*}"           # inline comment after whitespace
        value="${value%"${value##*[![:space:]]}"}" # trailing whitespace
        value="${value#\"}"; value="${value%\"}"
        value="${value#\'}"; value="${value%\'}"
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < "$env_file"
}

write_env_file() {
    local env_file="${REPO_DIR}/.env"
    [ ! -f "$env_file" ] || return 0
    [ -f "${REPO_DIR}/.env.example" ] || return 0
    (umask 077 && cp "${REPO_DIR}/.env.example" "$env_file")
    chmod 600 "$env_file" 2>/dev/null || true
    printf 'Wrote %s from .env.example (mode 600). Add your LLM credential there.\n' "$env_file"
}

# ==============================================================
# State root: secrets/token.json (GitHub credential metadata — the
# token itself lives in gh), mirrors/, checkouts/, workspaces/, jobs/,
# audit/, logs/. Never inside the checkout.
# ==============================================================
prepare_state_root() {
    local state_root="$1"
    case "${state_root}/" in
        "${REPO_DIR}/"*) fail "HEARTH_STATE_DIR (${state_root}) must not be inside the checkout (${REPO_DIR})." ;;
    esac
    mkdir -p "${state_root}/secrets" "${state_root}/logs" ||
        fail "Could not create the state root at ${state_root}."
    chmod 700 "${state_root}/secrets" 2>/dev/null || true
}

# ==============================================================
# Port: fail with something actionable instead of a traceback.
# ==============================================================
ensure_port_available() {
    local host="$1"
    local port="$2"
    local status=0

    HEARTH_CHECK_HOST="$host" HEARTH_CHECK_PORT="$port" "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1 || status=$?
import os, socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind((os.environ["HEARTH_CHECK_HOST"], int(os.environ["HEARTH_CHECK_PORT"])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY

    case "$status" in
        0) return 0 ;;
        1) fail "Port ${port} is already in use — Hearth-Core may already be running. Stop it with Ctrl-C in its terminal, or set PORT=<port> to start another." ;;
        *) printf 'Could not verify that port %s is free; starting anyway.\n' "$port" ;;
    esac
}

start_server() {
    local state_root="$1"
    local log_file="${state_root}/logs/hearth-core.log"

    export HOST PORT HEARTH_STATE_DIR="$state_root"
    export AGENT_NAME="${AGENT_NAME:-claude_code}"
    # A developer's machine, not the space image: install nothing at boot unless asked.
    export HEARTH_SKIP_BOOT_INSTALL="${HEARTH_SKIP_BOOT_INSTALL:-1}"
    export UVICORN_RELOAD="${UVICORN_RELOAD:-false}"

    printf '\n▶️  Starting Hearth-Core: http://%s:%s/health\n\n' "$HOST" "$PORT"
    printf '    Logs:  %s\n' "$log_file"
    printf '    Press Ctrl-C to stop.\n'
    printf '    Start again later:  cd %s && ./install.sh\n\n' "$REPO_DIR"

    # exec replaces this shell so Ctrl-C reaches Uvicorn directly.
    cd "$REPO_DIR"
    exec "$VENV_PYTHON" server.py >> "$log_file" 2>&1
}

main() {
    parse_args "$@"
    [ -n "${HOME:-}" ] || fail "HOME must be set."

    resolve_repo_dir
    require_command git \
        "git is not installed. Hearth-Core needs it to mirror repositories and push branches. Install git and run this script again."
    [ "$MANAGED_CHECKOUT" -eq 0 ] || fetch_repo
    cd "$REPO_DIR"

    # Before any default is applied, so .env drives the values below.
    load_env_file
    HOST="${HOST:-127.0.0.1}"
    PORT="${PORT:-5010}"
    local state_root="${HEARTH_STATE_DIR:-${HOME}/.hearth}"
    state_root="${state_root/#\~/$HOME}"

    ensure_uv
    sync_dependencies
    prepare_state_root "$state_root"
    write_env_file

    printf '\nHearth-Core source: %s (%s)\nHearth state:       %s\nPython:             %s\n' \
        "$REPO_DIR" "$([ "$MANAGED_CHECKOUT" -eq 1 ] && echo managed || echo in place)" "$state_root" "$VENV_PYTHON"
    check_tools

    if [ "$START_SERVER" -eq 0 ]; then
        printf '\nInstalled. Start it with: ./install.sh\n'
        return 0
    fi
    ensure_port_available "$HOST" "$PORT"
    start_server "$state_root"
}

main "$@"
