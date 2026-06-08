#Requires -Version 5.1
<#
.SYNOPSIS
    Codex <-> F5 AI Guardrails - Windows Installer

.DESCRIPTION
    Copies hook scripts to %USERPROFILE%\.codex\hooks\f5_guardrails\
    Installs hooks.json to %USERPROFILE%\.codex\hooks.json (backs up if exists)
    Ensures codex_hooks feature is enabled in config.toml
    Validates F5_GUARDRAILS_API_TOKEN and runs a smoke test scan
#>

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$HooksDir = Join-Path $CodexHome "hooks\f5_guardrails"
$ConfigFile = Join-Path $CodexHome "config.toml"
$HooksJson = Join-Path $CodexHome "hooks.json"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Info  { param([string]$Msg) Write-Host "[OK] $Msg" -ForegroundColor Green }
function Write-Warn  { param([string]$Msg) Write-Host "[!]  $Msg" -ForegroundColor Yellow }
function Write-Err   { param([string]$Msg) Write-Host "[X]  $Msg" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# Find Python — try 'python' first, then 'py' launcher
# ---------------------------------------------------------------------------

function Find-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            $null = & $cmd --version 2>&1
            if ($LASTEXITCODE -eq 0) { return $cmd }
        } catch {
            # Command not found — continue
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host ("=" * 62)
Write-Host "  Codex <-> F5 AI Guardrails - Windows Installer"
Write-Host ("=" * 62)
Write-Host ""

# Check Python
$PythonCmd = Find-Python
if (-not $PythonCmd) {
    Write-Err "Python is required but not found in PATH."
    Write-Host "  Install Python from https://www.python.org/downloads/"
    Write-Host "  or run: winget install Python.Python.3"
    exit 1
}

$PythonVersion = & $PythonCmd --version 2>&1
Write-Info "Python found: $PythonVersion (command: $PythonCmd)"

# Check requests module
$RequestsCheck = & $PythonCmd -c "import requests" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Python 'requests' module not found. Installing..."
    & $PythonCmd -m pip install -q requests python-dotenv
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to install Python dependencies."
        exit 1
    }
    Write-Info "Dependencies installed."
} else {
    Write-Info "Python 'requests' module available."
}

# Check API token
if (-not $env:F5_GUARDRAILS_API_TOKEN) {
    Write-Warn "F5_GUARDRAILS_API_TOKEN is not set."
    Write-Host "  Set it in PowerShell:"
    Write-Host '    $env:F5_GUARDRAILS_API_TOKEN = "your-token-here"'
    Write-Host ""
    Write-Host "  To persist across sessions, add it via:"
    Write-Host "    System Properties > Advanced > Environment Variables"
    Write-Host ""
    Write-Host "  Continuing installation - hooks will fail-open without a token."
    Write-Host ""
} else {
    Write-Info "F5_GUARDRAILS_API_TOKEN is set."
}

# ---------------------------------------------------------------------------
# Install hook scripts
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Installing hook scripts..."

New-Item -ItemType Directory -Force -Path $HooksDir | Out-Null

$HookFiles = @(
    "f5_guardrails_client.py",
    "user_prompt_submit.py",
    "pre_tool_use.py",
    "post_tool_use.py"
)

foreach ($file in $HookFiles) {
    $src = Join-Path $ScriptDir "hooks\$file"
    if (-not (Test-Path $src)) {
        Write-Err "Missing hook script: $src"
        exit 1
    }
    Copy-Item -Path $src -Destination (Join-Path $HooksDir $file) -Force
}

Write-Info "Hook scripts installed to $HooksDir"

# ---------------------------------------------------------------------------
# Install hooks.json (Windows version)
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Configuring Codex hooks..."

if (Test-Path $HooksJson) {
    Write-Warn "Existing hooks.json found at $HooksJson"
    Write-Host "  Backing up to ${HooksJson}.bak"
    Copy-Item -Path $HooksJson -Destination "${HooksJson}.bak" -Force
}

$WindowsHooksJson = Join-Path $ScriptDir "hooks-windows.json"
if (-not (Test-Path $WindowsHooksJson)) {
    Write-Err "Missing hooks-windows.json in $ScriptDir"
    exit 1
}
Copy-Item -Path $WindowsHooksJson -Destination $HooksJson -Force
Write-Info "hooks.json installed to $HooksJson"

# ---------------------------------------------------------------------------
# Ensure codex_hooks feature is enabled in config.toml
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null

if (Test-Path $ConfigFile) {
    $configContent = Get-Content $ConfigFile -Raw

    if ($configContent -match "codex_hooks") {
        if ($configContent -match "codex_hooks\s*=\s*false") {
            Write-Warn "codex_hooks is set to false in config.toml - updating to true."
            $configContent = $configContent -replace "codex_hooks\s*=\s*false", "codex_hooks = true"
            Set-Content -Path $ConfigFile -Value $configContent -NoNewline
        } else {
            Write-Info "codex_hooks already enabled in config.toml."
        }
    } else {
        if ($configContent -match "(?m)^\[features\]") {
            # Features section exists — append under it.
            $configContent = $configContent -replace "(?m)(^\[features\])", "`$1`ncodex_hooks = true"
            Set-Content -Path $ConfigFile -Value $configContent -NoNewline
        } else {
            # No features section — add it at the end.
            Add-Content -Path $ConfigFile -Value "`n[features]`ncodex_hooks = true"
        }
        Write-Info "Added codex_hooks = true to config.toml."
    }
} else {
    # No config.toml — create a minimal one.
    $minimalConfig = @"
# Codex configuration
# See: https://developers.openai.com/codex/local-config

[features]
codex_hooks = true
"@
    Set-Content -Path $ConfigFile -Value $minimalConfig
    Write-Info "Created config.toml with codex_hooks enabled."
}

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Running smoke test..."

if ($env:F5_GUARDRAILS_API_TOKEN) {
    $smokeScript = @"
import sys
sys.path.insert(0, r'$HooksDir')
from f5_guardrails_client import scan
result = scan('Hello, this is a test prompt.', context='smoke_test')
print(f'{result.outcome} ({result.duration_ms:.0f}ms)')
if result.is_error:
    print(f'  Error: {result.message}')
"@

    try {
        $smokeResult = & $PythonCmd -c $smokeScript 2>&1
        if ($smokeResult -match "cleared|passed") {
            Write-Info "Smoke test passed: $smokeResult"
        } elseif ($LASTEXITCODE -ne 0) {
            Write-Warn "Smoke test failed - check your API token and network connectivity."
        } else {
            Write-Info "Smoke test result: $smokeResult"
        }
    } catch {
        Write-Warn "Smoke test failed - check your API token and network connectivity."
    }
} else {
    Write-Warn "Skipping smoke test - no API token set."
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host ("=" * 62)
Write-Host "  Installation complete."
Write-Host ""
Write-Host "  Hook scripts:  $HooksDir"
Write-Host "  Hooks config:  $HooksJson"
Write-Host "  Codex config:  $ConfigFile"
Write-Host ""
Write-Host "  Required env var:"
Write-Host '    $env:F5_GUARDRAILS_API_TOKEN = "your-token-here"'
Write-Host ""
Write-Host "  Optional env vars:"
Write-Host "    F5_GUARDRAILS_BASE_URL       (default: https://www.us1.calypsoai.app)"
Write-Host "    F5_GUARDRAILS_PROJECT_ID     (scope scans to a specific F5 project)"
Write-Host "    F5_GUARDRAILS_TIMEOUT        (default: 10 seconds)"
Write-Host "    F5_GUARDRAILS_FAIL_MODE      (default: open - set to 'closed' for strict)"
Write-Host "    F5_GUARDRAILS_POST_STRICT    (default: false - set to 'true' to block on flagged output)"
Write-Host "    F5_GUARDRAILS_LOG_LEVEL      (default: warn - debug|info|warn|error)"
Write-Host "    F5_GUARDRAILS_MAX_SCAN_LENGTH (default: 50000 chars)"
Write-Host ""
Write-Host "  Restart Codex (CLI, app, or IDE extension) for hooks to take effect."
Write-Host ("=" * 62)
