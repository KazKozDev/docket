#!/bin/bash
# Double-click this file in Finder to bring up the whole stack:
# Ollama, the FastAPI service, and the Streamlit UI in a browser.
#
# It is safe to run repeatedly — it frees its own ports first, so a
# previous run that was closed by clicking the X on the Terminal window
# (which leaves the servers orphaned) won't block this one.

cd "$(dirname "$0")" || exit 1

API_PORT=8000
UI_PORT=8501
OLLAMA_PORT=11434
PIDS=()

say() { printf "\033[1;36m==>\033[0m %s\n" "$1"; }
ok()  { printf "\033[1;32m  OK\033[0m   %s\n" "$1"; }
warn() { printf "\033[1;33m  WARN\033[0m %s\n" "$1"; }

cleanup() {
    echo
    say "Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    ok "Stopped. This window can be closed."
    sleep 1
}
trap cleanup EXIT INT TERM

free_port() {
    local port=$1
    local pids
    pids=$(lsof -ti tcp:"$port" 2>/dev/null)
    if [ -n "$pids" ]; then
        warn "port $port was busy — freeing it (pid $(echo "$pids" | tr '\n' ' '))"
        echo "$pids" | xargs kill -9 2>/dev/null
        sleep 1
    fi
}

wait_for_port() {
    local port=$1 label=$2 tries=${3:-40}
    for _ in $(seq 1 "$tries"); do
        if lsof -ti tcp:"$port" >/dev/null 2>&1; then
            ok "$label is up on port $port"
            return 0
        fi
        sleep 0.5
    done
    warn "$label did not come up on port $port — check the output above"
    return 1
}

printf "\033[1m\n  docket — starting up\n\033[0m\n"

# ---------------------------------------------------------------- venv
say "Checking the Python environment"
if [ ! -d .venv ]; then
    warn "no .venv found — creating one (this takes a minute the first time)"
    python3 -m venv .venv || { warn "could not create a venv — is python3 installed?"; read -r; exit 1; }
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -e ".[dev]" || { warn "dependency install failed"; read -r; exit 1; }
    ok "environment created"
else
    ./.venv/bin/python -c "import docket, fastapi, streamlit" 2>/dev/null || {
        warn "dependencies are missing or stale — installing"
        ./.venv/bin/pip install --quiet -e ".[dev]"
    }
    ok "environment ready"
fi

# -------------------------------------------------------------- tesseract
if ! command -v tesseract >/dev/null 2>&1; then
    warn "tesseract is not installed — scanned documents will fall back to the vision model"
    warn "install it with:  brew install tesseract"
else
    ok "tesseract found"
fi

# ---------------------------------------------------------------- ollama
say "Checking Ollama"
if ! command -v ollama >/dev/null 2>&1; then
    warn "ollama is not installed — get it from https://ollama.com"
    warn "the UI will start, but extraction will fail without it"
elif curl -s "http://localhost:$OLLAMA_PORT/api/tags" >/dev/null 2>&1; then
    ok "Ollama already running"
else
    warn "Ollama not running — starting it"
    ollama serve >/tmp/docket-ollama.log 2>&1 &
    PIDS+=($!)
    wait_for_port "$OLLAMA_PORT" "Ollama"
fi

# ------------------------------------------------------------------ ports
say "Freeing ports"
free_port "$API_PORT"
free_port "$UI_PORT"
ok "ports $API_PORT and $UI_PORT are clear"

# -------------------------------------------------------------------- api
say "Starting the API"
./.venv/bin/uvicorn docket.api:app --host 127.0.0.1 --port "$API_PORT" >/tmp/docket-api.log 2>&1 &
PIDS+=($!)
wait_for_port "$API_PORT" "API"
echo "      docs:  http://localhost:$API_PORT/docs"
echo "      log:   /tmp/docket-api.log"

# --------------------------------------------------------------------- ui
say "Starting the UI (a browser tab will open)"
echo
printf "\033[1;32m  Everything is up. Press Ctrl+C here to stop it all.\033[0m\n\n"

./.venv/bin/streamlit run examples/streamlit_demo.py \
    --server.port "$UI_PORT" \
    --server.headless false \
    --browser.gatherUsageStats false
