#!/bin/bash
# =============================================================================
# setup.sh — Drona-MCL bootstrap script
#
# Actions:
#   1. Verify prerequisites (python3, bash, ollama CLI)
#   2. Detect Python version; install tomli if < 3.11
#   3. Create a virtual environment (.venv)
#   4. Install Python dependencies from requirements.txt
#   5. Pull the configured Ollama model
#   6. Create ai_workspace if it doesn't exist
#   7. Run the test suite to verify the installation
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
CONFIG="${SCRIPT_DIR}/config/config.toml"
AI_WORKSPACE="${SCRIPT_DIR}/ai_workspace"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'  # No Colour

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

# ── 1. Prerequisite checks ────────────────────────────────────────────────────
info "Checking prerequisites..."

command -v python3 &>/dev/null || die "python3 not found. Install Python 3.10+."
command -v bash    &>/dev/null || die "bash not found."

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PYTHON_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

info "Python version: ${PYTHON_VERSION}"

if [ "${PYTHON_MAJOR}" -lt 3 ] || { [ "${PYTHON_MAJOR}" -eq 3 ] && [ "${PYTHON_MINOR}" -lt 10 ]; }; then
    die "Drona requires Python 3.10 or later (found ${PYTHON_VERSION})."
fi

# Check for Ollama CLI (optional — warns, doesn't abort)
if command -v ollama &>/dev/null; then
    OLLAMA_VERSION=$(ollama --version 2>/dev/null | head -1 || echo "unknown")
    info "Ollama CLI found: ${OLLAMA_VERSION}"
else
    warn "Ollama CLI not found. You must install Ollama separately."
    warn "Visit: https://ollama.com/download"
fi

# ── 2. Virtual environment ────────────────────────────────────────────────────
if [ -d "${VENV_DIR}" ]; then
    info "Virtual environment already exists at ${VENV_DIR}. Skipping creation."
else
    info "Creating virtual environment at ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
fi

# Activate venv for the rest of this script
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"

# ── 3. Python < 3.11: install tomli ──────────────────────────────────────────
if [ "${PYTHON_MINOR}" -lt 11 ] && [ "${PYTHON_MAJOR}" -eq 3 ]; then
    info "Python < 3.11 detected — installing tomli (tomllib backport)..."
    pip install --quiet "tomli>=2.0.0"
fi

# ── 4. Install dependencies ───────────────────────────────────────────────────
info "Installing dependencies from requirements.txt..."
pip install --quiet --upgrade pip
pip install --quiet -r "${REQUIREMENTS}"
info "Dependencies installed."

# ── 5. Pull Ollama model ──────────────────────────────────────────────────────
if command -v ollama &>/dev/null; then
    # Extract model name from config.toml using Python (no external tools needed)
    MODEL=$(python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
from core.config_loader import load_config
cfg = load_config()
print(cfg.ollama.model)
")
    info "Pulling Ollama model: ${MODEL}"
    info "(This may take a while on first run — the model is downloaded once and cached.)"
    ollama pull "${MODEL}" || warn "Could not pull model '${MODEL}'. Start Ollama and run 'ollama pull ${MODEL}' manually."
else
    warn "Skipping model pull (Ollama CLI not available)."
fi

# ── 6. Ensure ai_workspace exists ─────────────────────────────────────────────
if [ ! -d "${AI_WORKSPACE}" ]; then
    info "Creating ai_workspace directory..."
    mkdir -p "${AI_WORKSPACE}"
fi
info "ai_workspace: ${AI_WORKSPACE}"

# ── 7. Run test suite ─────────────────────────────────────────────────────────
info "Running test suite..."
cd "${SCRIPT_DIR}"

# Run tests; capture exit code without aborting immediately
set +e
python3 -m pytest tests/ -v --tb=short
TEST_EXIT=$?
set -e

if [ "${TEST_EXIT}" -eq 0 ]; then
    info "All tests passed. ✓"
else
    warn "Some tests failed (exit code ${TEST_EXIT})."
    warn "Review the output above. This may be expected if Ollama is not running."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Setup complete. To activate the environment in your shell:"
echo "    source ${VENV_DIR}/bin/activate"
echo ""
info "To run Drona:"
echo "    python main.py \"Your sysadmin task here\""
