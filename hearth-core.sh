#!/usr/bin/env bash
# hearth-core.sh — run Hearth-Core locally.
#
#   ./hearth-core.sh install   ./install.sh --no-start (venv + deps, no server)
#   ./hearth-core.sh dev       run the API with auto-reload
#   ./hearth-core.sh test      run the unittest gate
#
# Shape mirrors xo-space cowork-api.sh.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY=venv/bin/python
[[ -x "$PY" ]] || PY=venv/Scripts/python.exe   # Windows venv layout

case "${1:-}" in
  install)
    exec ./install.sh --no-start
    ;;
  dev)
    UVICORN_RELOAD=true exec "$PY" server.py
    ;;
  test)
    exec "$PY" -m unittest discover -s tests -t .
    ;;
  *)
    echo "usage: $0 install|dev|test" >&2
    exit 2
    ;;
esac
