#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  Agent - Startup Script"
echo "========================================"
echo

if [[ ! -f ".env" ]]; then
  if [[ ! -f ".env.example" ]]; then
    echo "[ERROR] .env.example not found."
    exit 1
  fi

  cp ".env.example" ".env"
  rand_suffix="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(4))
PY
)"
  random_agent_name="Agent_${rand_suffix}"

  tmp_file="$(mktemp)"
  awk -v agent_name="$random_agent_name" '
    BEGIN { has_agent_name=0; has_admin=0 }
    /^AGENT_NAME=/ {
      print "AGENT_NAME=" agent_name
      has_agent_name=1
      next
    }
    /^AGENT_DEFAULT_ADMIN_USERNAME=/ {
      print "AGENT_DEFAULT_ADMIN_USERNAME="
      has_admin=1
      next
    }
    { print }
    END {
      if (has_agent_name == 0) print "AGENT_NAME=" agent_name
      if (has_admin == 0) print "AGENT_DEFAULT_ADMIN_USERNAME="
    }
  ' ".env" > "$tmp_file"
  mv "$tmp_file" ".env"

  echo "[INFO] .env created: AGENT_NAME=${random_agent_name}, AGENT_DEFAULT_ADMIN_USERNAME=<empty>"
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "[ERROR] Python not found in PATH."
  exit 1
fi

if [[ ! -x "venv/bin/python" ]]; then
  echo "[INFO] Creating virtual environment..."
  "$PYTHON_BIN" -m venv venv || true
fi

if [[ -x "venv/bin/python" ]]; then
  PYTHON_EXE="venv/bin/python"
else
  PYTHON_EXE="$PYTHON_BIN"
fi

echo "[INFO] Using Python: $PYTHON_EXE"
if [[ "$PYTHON_EXE" == "$PYTHON_BIN" ]]; then
  echo "[WARN] Running with system Python."
  echo "[WARN] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, self-update will install deps into system Python."
else
  echo "[INFO] Running with virtual environment Python."
  echo "[INFO] If AGENT_AUTO_UPDATE_INSTALL_DEPS=true, deps will be installed into this venv."
fi
echo "[INFO] Installing dependencies..."
"$PYTHON_EXE" -m pip install --upgrade pip >/dev/null || true
"$PYTHON_EXE" -m pip install --prefer-binary -r requirements.txt

echo
echo "========================================"
echo "  Starting Agent..."
echo "  Press Ctrl+C to stop"
echo "========================================"
echo

exec "$PYTHON_EXE" start_agent.py
