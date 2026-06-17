#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Codex ↔ F5 AI Guardrails — Installer
#
# Copies hook scripts to ~/.codex/hooks/f5_guardrails/
# Installs hooks.json to ~/.codex/hooks.json (merges if exists)
# Ensures codex_hooks feature is enabled in config.toml
# Validates F5_GUARDRAILS_API_TOKEN and runs a smoke test scan
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
HOOKS_DIR="$CODEX_HOME/hooks/f5_guardrails"
CONFIG_FILE="$CODEX_HOME/config.toml"
HOOKS_JSON="$CODEX_HOME/hooks.json"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }

# --- Pre-flight checks ---

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Codex ↔ F5 AI Guardrails — Installer"
echo "══════════════════════════════════════════════════════════════"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    error "python3 is required but not found in PATH."
    exit 1
fi
info "Python3 found: $(python3 --version)"

# Check Python dependencies
if ! python3 -c "import requests, truststore" 2>/dev/null; then
    warn "Python dependencies not found. Installing..."
    pip3 install -q requests python-dotenv truststore
    info "Dependencies installed."
else
    info "Python dependencies available."
fi

# Check API token
if [ -z "${F5_GUARDRAILS_API_TOKEN:-}" ]; then
    warn "F5_GUARDRAILS_API_TOKEN is not set."
    echo "  Set it in your shell profile:"
    echo "    export F5_GUARDRAILS_API_TOKEN=\"your-token-here\""
    echo ""
    echo "  Continuing installation — hooks will fail-open without a token."
    echo ""
else
    info "F5_GUARDRAILS_API_TOKEN is set."
fi

# --- Install hook scripts ---

echo ""
echo "Installing hook scripts..."

mkdir -p "$HOOKS_DIR"
cp "$SCRIPT_DIR/hooks/f5_guardrails_client.py" "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/user_prompt_submit.py"   "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/pre_tool_use.py"         "$HOOKS_DIR/"
cp "$SCRIPT_DIR/hooks/post_tool_use.py"        "$HOOKS_DIR/"
chmod +x "$HOOKS_DIR"/*.py

info "Hook scripts installed to $HOOKS_DIR"

# --- Install hooks.json ---

echo ""
echo "Configuring Codex hooks..."

if [ -f "$HOOKS_JSON" ]; then
    warn "Existing hooks.json found at $HOOKS_JSON"
    echo "  Backing up to ${HOOKS_JSON}.bak"
    cp "$HOOKS_JSON" "${HOOKS_JSON}.bak"
fi

cp "$SCRIPT_DIR/hooks.json" "$HOOKS_JSON"
info "hooks.json installed to $HOOKS_JSON"

# --- Ensure codex_hooks feature is enabled in config.toml ---

mkdir -p "$CODEX_HOME"

if [ -f "$CONFIG_FILE" ]; then
    if grep -q "codex_hooks" "$CONFIG_FILE"; then
        # Already has the key — make sure it's true.
        if grep -q "codex_hooks = false" "$CONFIG_FILE"; then
            warn "codex_hooks is set to false in config.toml — updating to true."
            sed -i.bak 's/codex_hooks = false/codex_hooks = true/' "$CONFIG_FILE"
        else
            info "codex_hooks already enabled in config.toml."
        fi
    else
        # No codex_hooks key — add the features section.
        if grep -q '^\[features\]' "$CONFIG_FILE"; then
            # Features section exists — append under it.
            sed -i.bak '/^\[features\]/a\
codex_hooks = true' "$CONFIG_FILE"
        else
            # No features section — add it at the end.
            echo "" >> "$CONFIG_FILE"
            echo "[features]" >> "$CONFIG_FILE"
            echo "codex_hooks = true" >> "$CONFIG_FILE"
        fi
        info "Added codex_hooks = true to config.toml."
    fi
else
    # No config.toml — create a minimal one.
    cat > "$CONFIG_FILE" <<EOF
# Codex configuration
# See: https://developers.openai.com/codex/local-config

[features]
codex_hooks = true
EOF
    info "Created config.toml with codex_hooks enabled."
fi

# --- Smoke test ---

echo ""
echo "Running smoke test..."

if [ -n "${F5_GUARDRAILS_API_TOKEN:-}" ]; then
    SMOKE_RESULT=$(python3 -c "
import sys
sys.path.insert(0, '$HOOKS_DIR')
from f5_guardrails_client import scan
result = scan('Hello, this is a test prompt.', context='smoke_test')
print(f'{result.outcome} ({result.duration_ms:.0f}ms)')
if result.is_error:
    print(f'  Error: {result.message}')
" 2>/dev/null || echo "FAILED")

    if echo "$SMOKE_RESULT" | grep -q "cleared\|passed"; then
        info "Smoke test passed: $SMOKE_RESULT"
    elif echo "$SMOKE_RESULT" | grep -q "FAILED"; then
        warn "Smoke test failed — check your API token and network connectivity."
    else
        info "Smoke test result: $SMOKE_RESULT"
    fi
else
    warn "Skipping smoke test — no API token set."
fi

# --- Done ---

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  Installation complete."
echo ""
echo "  Hook scripts:  $HOOKS_DIR"
echo "  Hooks config:  $HOOKS_JSON"
echo "  Codex config:  $CONFIG_FILE"
echo ""
echo "  Required env var:"
echo "    export F5_GUARDRAILS_API_TOKEN=\"your-token-here\""
echo ""
echo "  Optional env vars:"
echo "    F5_GUARDRAILS_BASE_URL       (default: https://www.us1.calypsoai.app)"
echo "    F5_GUARDRAILS_PROJECT_ID     (scope scans to a specific F5 project)"
echo "    F5_GUARDRAILS_TIMEOUT        (default: 10 seconds)"
echo "    F5_GUARDRAILS_FAIL_MODE      (default: open — set to 'closed' for strict)"
echo "    F5_GUARDRAILS_POST_STRICT    (default: false — set to 'true' to block on flagged output)"
echo "    F5_GUARDRAILS_LOG_LEVEL      (default: warn — debug|info|warn|error)"
echo "    F5_GUARDRAILS_MAX_SCAN_LENGTH (default: 50000 chars)"
echo "    F5_GUARDRAILS_USE_SYSTEM_CERT_STORE (default: auto — Windows Cert Store when available)"
echo ""
echo "  Restart Codex (CLI, app, or IDE extension) for hooks to take effect."
echo "══════════════════════════════════════════════════════════════"
