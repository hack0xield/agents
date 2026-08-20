#!/usr/bin/env bash
# Run the local Ollama server (user-local install, no sudo, no systemd).
set -euo pipefail
export PATH="$HOME/.local/ollama/bin:$PATH"
export LD_LIBRARY_PATH="$HOME/.local/ollama/lib:${LD_LIBRARY_PATH:-}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/.local/ollama/models}"

trap 'echo; echo "[ollama] stopping…"; kill "${PID:-}" 2>/dev/null; exit 0' INT TERM
ollama serve &
PID=$!
wait "$PID"
