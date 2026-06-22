#Requires -Version 5.1
# Copyright 2026 Andy Ryan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

<#
.SYNOPSIS
    Codex <-> F5 AI Guardrails - Windows Installer

.DESCRIPTION
    Copies hook scripts to %USERPROFILE%\.codex\hooks\f5_guardrails\
    Enables the current Codex hooks feature flag in %USERPROFILE%\.codex\config.toml
    Installs Windows managed hook configuration to C:\ProgramData\OpenAI\Codex\requirements.toml
    Disables user-level hooks.json by default to prevent duplicate hook execution
    Calls smoketest.py after install, instead of embedding the smoke test inline
    Defaults PreToolUse/PostToolUse matcher to ".*" for Codex Windows GUI/app tool names

.NOTES
    Run from an elevated / Administrator PowerShell prompt so the installer can write:
      C:\ProgramData\OpenAI\Codex\requirements.toml

.PARAMETER InstallUserHooksJson
    Also install %USERPROFILE%\.codex\hooks.json for legacy/user-level CLI hooks.
    Not recommended for the Windows GUI managed-hook path because it can double-scan.

.PARAMETER SkipSmokeTest
    Skip running smoketest.py after installation.

.PARAMETER PreserveExistingLogFile
    Do not reset F5_GUARDRAILS_LOG_FILE to %USERPROFILE%\.codex\logs\f5_guardrails.log.

.PARAMETER ToolMatcher
    Matcher for PreToolUse and PostToolUse managed hooks.
    Defaults to .* because Codex Windows GUI/app tool names are broader than Bash/apply_patch.
#>

param(
    [switch]$InstallUserHooksJson,
    [switch]$SkipSmokeTest,
    [switch]$PreserveExistingLogFile,
    [string]$ToolMatcher = ".*"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE ".codex" }
$HooksDir = Join-Path $CodexHome "hooks\f5_guardrails"
$ConfigFile = Join-Path $CodexHome "config.toml"
$HooksJson = Join-Path $CodexHome "hooks.json"
$ManagedConfigDir = "C:\ProgramData\OpenAI\Codex"
$ManagedRequirements = Join-Path $ManagedConfigDir "requirements.toml"
$DefaultLogFile = Join-Path $CodexHome "logs\f5_guardrails.log"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

function Write-Info { param([string]$Msg) Write-Host "[OK] $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "[!]  $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[X]  $Msg" -ForegroundColor Red }

function Write-CapturedOutput {
    param($Output)

    if (-not $Output) {
        return
    }

    foreach ($line in $Output) {
        Write-Host ([string]$line)
    }
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Escape-TomlSingleQuoted {
    param([string]$Value)

    # TOML literal strings are single-quoted. A single quote inside the value
    # must be represented as two single quotes.
    return $Value -replace "'", "''"
}

function Find-PythonExe {
    try {
        $exe = & python -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) {
            return $exe.Trim()
        }
    } catch {
        # try py launcher
    }

    try {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) {
            return $exe.Trim()
        }
    } catch {
        # not found
    }

    return $null
}

function Get-ShortPath {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        throw "Cannot resolve short path because path does not exist: $Path"
    }

    $fso = New-Object -ComObject Scripting.FileSystemObject
    $item = Get-Item -LiteralPath $Path

    if ($item.PSIsContainer) {
        return $fso.GetFolder($item.FullName).ShortPath
    }

    return $fso.GetFile($item.FullName).ShortPath
}

function Get-CommandSafePath {
    param([string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if ($resolved -notmatch "\s") {
        return $resolved
    }

    $shortPath = Get-ShortPath -Path $resolved
    if ($shortPath -and $shortPath -notmatch "\s") {
        return $shortPath
    }

    throw "Path contains spaces and no usable 8.3 short path was found: $resolved"
}

function New-HookCommandWindows {
    param(
        [string]$PythonExe,
        [string]$ScriptPath
    )

    $safePythonExe = Get-CommandSafePath -Path $PythonExe
    $safeScriptPath = Get-CommandSafePath -Path $ScriptPath

    return Escape-TomlSingleQuoted ("$safePythonExe $safeScriptPath")
}

function Set-TomlKey {
    param(
        [string]$Path,
        [string]$Section,
        [string]$Key,
        [string]$Value
    )

    if (-not (Test-Path $Path)) {
        Set-Content -Path $Path -Value "[$Section]`n$Key = $Value`n" -Encoding UTF8
        return
    }

    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($line in (Get-Content $Path)) {
        [void]$lines.Add($line)
    }

    $sectionHeader = "[$Section]"
    $sectionIndex = -1

    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim() -eq $sectionHeader) {
            $sectionIndex = $i
            break
        }
    }

    if ($sectionIndex -lt 0) {
        if ($lines.Count -gt 0 -and $lines[$lines.Count - 1].Trim() -ne "") {
            [void]$lines.Add("")
        }
        [void]$lines.Add($sectionHeader)
        [void]$lines.Add("$Key = $Value")
        Set-Content -Path $Path -Value $lines -Encoding UTF8
        return
    }

    $endIndex = $lines.Count
    for ($i = $sectionIndex + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i].Trim().StartsWith("[")) {
            $endIndex = $i
            break
        }
    }

    $keyIndex = -1
    $keyRegex = "^\s*" + [regex]::Escape($Key) + "\s*="
    for ($i = $sectionIndex + 1; $i -lt $endIndex; $i++) {
        if ($lines[$i] -match $keyRegex) {
            $keyIndex = $i
            break
        }
    }

    if ($keyIndex -ge 0) {
        $lines[$keyIndex] = "$Key = $Value"
    } else {
        $lines.Insert($sectionIndex + 1, "$Key = $Value")
    }

    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

function Remove-DeprecatedCodexHooksFlag {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    $content = Get-Content $Path -Raw
    $content = $content -replace "(?m)^\s*codex_hooks\s*=.*\r?\n?", ""
    Set-Content -Path $Path -Value $content -NoNewline -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host ("=" * 72)
Write-Host "  Codex <-> F5 AI Guardrails - Windows Installer"
Write-Host ("=" * 72)
Write-Host ""

if (-not (Test-IsAdmin)) {
    Write-Err "This installer must be run as Administrator."
    Write-Host ""
    Write-Host "Reason: the Windows GUI managed-hook config is written to:"
    Write-Host "  $ManagedRequirements"
    Write-Host ""
    Write-Host "Open PowerShell as Administrator, then run:"
    Write-Host "  cd $ScriptDir"
    Write-Host "  .\install.ps1"
    exit 1
}

$PythonExe = Find-PythonExe
if (-not $PythonExe) {
    Write-Err "Python is required but was not found."
    Write-Host "  Install Python, then reopen PowerShell."
    Write-Host "  Example:"
    Write-Host "    winget install 9NQ7512CXL7T -e"
    exit 1
}

$PythonVersion = & $PythonExe --version 2>&1
Write-Info "Python found: $PythonVersion"
Write-Info "Python executable: $PythonExe"

# Check Python dependencies
$RequestsCheck = & $PythonExe -c "import requests, truststore" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warn "Python dependencies not found. Installing requests, python-dotenv, and truststore..."
    & $PythonExe -m pip install -q requests python-dotenv truststore
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to install Python dependencies."
        exit 1
    }
    Write-Info "Python dependencies installed."
} else {
    Write-Info "Python dependencies available."
}

if (-not $env:F5_GUARDRAILS_API_TOKEN) {
    Write-Warn "F5_GUARDRAILS_API_TOKEN is not set in this session."
    Write-Host ""
    Write-Host "Set it without printing the value:"
    Write-Host '  $token = Read-Host "Paste F5 Guardrails API token" -AsSecureString'
    Write-Host '  $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($token))'
    Write-Host '  $env:F5_GUARDRAILS_API_TOKEN = $plain'
    Write-Host '  [Environment]::SetEnvironmentVariable("F5_GUARDRAILS_API_TOKEN", $plain, "User")'
    Write-Host ""
    Write-Warn "Continuing installation. Hooks will fail-open without a token unless fail-closed is configured."
} else {
    Write-Info "F5_GUARDRAILS_API_TOKEN is set in this session."
}

# Keep CODEX_HOME explicit for GUI / child process inheritance.
$env:CODEX_HOME = $CodexHome
[Environment]::SetEnvironmentVariable("CODEX_HOME", $CodexHome, "User")
Write-Info "CODEX_HOME set to $CodexHome"

if (-not $PreserveExistingLogFile) {
    New-Item -ItemType Directory -Force -Path (Split-Path $DefaultLogFile) | Out-Null
    $env:F5_GUARDRAILS_LOG_FILE = $DefaultLogFile
    [Environment]::SetEnvironmentVariable("F5_GUARDRAILS_LOG_FILE", $DefaultLogFile, "User")
    Write-Info "F5_GUARDRAILS_LOG_FILE set to $DefaultLogFile"
} else {
    Write-Warn "Preserving existing F5_GUARDRAILS_LOG_FILE setting."
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
    "post_tool_use.py",
    "stop.py"
)

foreach ($file in $HookFiles) {
    $src = Join-Path $ScriptDir "hooks\$file"
    if (-not (Test-Path $src)) {
        Write-Err "Missing hook script: $src"
        exit 1
    }

    Copy-Item -Path $src -Destination (Join-Path $HooksDir $file) -Force
    Write-Info "Installed $file"
}

Write-Info "Hook scripts installed to $HooksDir"

# ---------------------------------------------------------------------------
# User-level hooks.json
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Configuring user-level hooks.json..."

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

if ($InstallUserHooksJson) {
    $WindowsHooksJson = Join-Path $ScriptDir "hooks-windows.json"
    if (-not (Test-Path $WindowsHooksJson)) {
        Write-Err "Missing hooks-windows.json in $ScriptDir"
        exit 1
    }

    if (Test-Path $HooksJson) {
        Copy-Item -Path $HooksJson -Destination "$HooksJson.bak-$Stamp" -Force
        Write-Warn "Existing hooks.json backed up to $HooksJson.bak-$Stamp"
    }

    Copy-Item -Path $WindowsHooksJson -Destination $HooksJson -Force
    Write-Warn "Installed hooks.json for user-level hooks."
    Write-Warn "If managed hooks are also active, this can cause duplicate scans."
} else {
    if (Test-Path $HooksJson) {
        Copy-Item -Path $HooksJson -Destination "$HooksJson.bak-$Stamp" -Force
        Move-Item -Path $HooksJson -Destination "$HooksJson.disabled" -Force
        Write-Info "Disabled user-level hooks.json to avoid duplicate scans."
        Write-Info "Backup saved to $HooksJson.bak-$Stamp"
        Write-Info "Disabled copy moved to $HooksJson.disabled"
    } else {
        Write-Info "No user-level hooks.json present. Managed hooks will be the active path."
    }
}

# ---------------------------------------------------------------------------
# Configure user config.toml
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Configuring Codex user config.toml..."

New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null

if (-not (Test-Path $ConfigFile)) {
    Set-Content -Path $ConfigFile -Value "" -Encoding UTF8
}

Remove-DeprecatedCodexHooksFlag -Path $ConfigFile
Set-TomlKey -Path $ConfigFile -Section "features" -Key "hooks" -Value "true"
Set-TomlKey -Path $ConfigFile -Section "features" -Key "js_repl" -Value "false"
Set-TomlKey -Path $ConfigFile -Section "windows" -Key "sandbox" -Value '"elevated"'

Write-Info "Updated $ConfigFile"
Write-Info "Ensured [features].hooks = true"
Write-Info "Ensured [windows].sandbox = `"elevated`""

# ---------------------------------------------------------------------------
# Configure managed Windows requirements.toml
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Configuring Windows managed hooks..."

New-Item -ItemType Directory -Force -Path $ManagedConfigDir | Out-Null

if (Test-Path $ManagedRequirements) {
    Copy-Item -Path $ManagedRequirements -Destination "$ManagedRequirements.bak-$Stamp" -Force
    Write-Warn "Existing requirements.toml backed up to $ManagedRequirements.bak-$Stamp"
}

$TomlHooksDir = Escape-TomlSingleQuoted $HooksDir

$UserPromptCommandWindows = New-HookCommandWindows -PythonExe $PythonExe -ScriptPath (Join-Path $HooksDir "user_prompt_submit.py")
$PreToolCommandWindows = New-HookCommandWindows -PythonExe $PythonExe -ScriptPath (Join-Path $HooksDir "pre_tool_use.py")
$PostToolCommandWindows = New-HookCommandWindows -PythonExe $PythonExe -ScriptPath (Join-Path $HooksDir "post_tool_use.py")
$StopCommandWindows = New-HookCommandWindows -PythonExe $PythonExe -ScriptPath (Join-Path $HooksDir "stop.py")

# Important:
# - Do not set allow_managed_hooks_only here. In testing, the stable Windows GUI path
#   was managed hooks in requirements.toml plus no user hooks.json.
# - Use command_windows and windows_managed_dir for native Windows Codex GUI.
# - Use a broad matcher by default because GUI/app tool names are broader than Bash/apply_patch.
# - Do not wrap command_windows components in double quotes. Codex Windows GUI
#   has hung with quoted command strings; use 8.3 short paths when spaces exist.
$ManagedToml = @"
# Codex managed requirements for F5 AI Guardrails hooks.
# Installed by install.ps1.
#
# Stable Windows GUI pattern:
#   - managed hooks live in this requirements.toml
#   - user-level hooks.json is disabled by default to avoid duplicate scans
#   - do not add allow_managed_hooks_only unless separately tested in your environment
#   - PreToolUse/PostToolUse default to matcher = ".*" for the Windows GUI/app path
#   - command_windows values intentionally avoid embedded quotes; install.ps1
#     resolves 8.3 short paths when needed to avoid spaces

[features]
hooks = true

[hooks]
managed_dir = "/enterprise/hooks"
windows_managed_dir = '$TomlHooksDir'

[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "python3 /enterprise/hooks/user_prompt_submit.py"
command_windows = '$UserPromptCommandWindows'
timeout = 15
statusMessage = "F5 Guardrails: managed prompt scan"

[[hooks.PreToolUse]]
matcher = "$ToolMatcher"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "python3 /enterprise/hooks/pre_tool_use.py"
command_windows = '$PreToolCommandWindows'
timeout = 15
statusMessage = "F5 Guardrails: managed tool-input scan"

[[hooks.PostToolUse]]
matcher = "$ToolMatcher"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "python3 /enterprise/hooks/post_tool_use.py"
command_windows = '$PostToolCommandWindows'
timeout = 15
statusMessage = "F5 Guardrails: managed tool-output scan"

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "python3 /enterprise/hooks/stop.py"
command_windows = '$StopCommandWindows'
timeout = 15
statusMessage = "F5 Guardrails: scanning assistant response"
"@

Set-Content -Path $ManagedRequirements -Value $ManagedToml -Encoding UTF8
Write-Info "Managed requirements installed to $ManagedRequirements"
Write-Info "Managed hooks configured with windows_managed_dir and command_windows"

# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "Running smoke test..."

if ($SkipSmokeTest) {
    Write-Warn "Skipping smoke test because -SkipSmokeTest was provided."
} elseif (-not $env:F5_GUARDRAILS_API_TOKEN) {
    Write-Warn "Skipping smoke test because F5_GUARDRAILS_API_TOKEN is not set in this session."
} else {
    $SmokeTest = Join-Path $ScriptDir "smoketest.py"

    if (-not (Test-Path $SmokeTest)) {
        Write-Warn "smoketest.py not found at $SmokeTest"
        Write-Warn "Skipping smoke test. Installation still completed."
    } else {
        try {
            Push-Location $ScriptDir
            $smokeOutput = & $PythonExe $SmokeTest 2>&1
            $smokeExit = $LASTEXITCODE
            Pop-Location

            if ($smokeExit -eq 0) {
                Write-Info "smoketest.py completed successfully."
                Write-CapturedOutput $smokeOutput
            } else {
                Write-Warn "smoketest.py exited with code $smokeExit."
                Write-CapturedOutput $smokeOutput
            }
        } catch {
            try { Pop-Location } catch {}
            Write-Warn "smoketest.py failed: $($_.Exception.Message)"
        }
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

Write-Host ""
Write-Host ("=" * 72)
Write-Host "  Installation complete."
Write-Host ""
Write-Host "  Hook scripts:              $HooksDir"
Write-Host "  Codex user config:         $ConfigFile"
Write-Host "  Managed requirements:      $ManagedRequirements"
Write-Host "  User hooks.json:           $(if (Test-Path $HooksJson) { $HooksJson } else { 'disabled / absent by default' })"
Write-Host "  Log file:                  $DefaultLogFile"
Write-Host ""
Write-Host "  Response scanning:"
Write-Host "    Installed through the Codex Stop hook."
Write-Host "    Scans Stop.last_assistant_message before display."
Write-Host "    Local/client-side response scanning; not upstream model proxy enforcement."
Write-Host ""
Write-Host "  Required env var:"
Write-Host '    F5_GUARDRAILS_API_TOKEN'
Write-Host ""
Write-Host "  Optional env vars:"
Write-Host "    F5_GUARDRAILS_BASE_URL         default: https://www.us1.calypsoai.app"
Write-Host "    F5_GUARDRAILS_PROJECT_ID       scope scans to a specific F5 project"
Write-Host "    F5_GUARDRAILS_TIMEOUT          default: 10 seconds"
Write-Host "    F5_GUARDRAILS_FAIL_MODE        default: open; set to 'closed' for strict"
Write-Host "    F5_GUARDRAILS_POST_STRICT      default: false; set to 'true' to block on flagged output"
Write-Host "    F5_GUARDRAILS_LOG_LEVEL        debug|info|warn|error"
Write-Host "    F5_GUARDRAILS_MAX_SCAN_LENGTH  default: 50000 chars"
Write-Host "    F5_GUARDRAILS_USE_SYSTEM_CERT_STORE  auto|true|false; default auto uses Windows Cert Store"
Write-Host "    F5_GUARDRAILS_CA_BUNDLE        CA bundle for TLS inspection"
Write-Host "    REQUESTS_CA_BUNDLE             requests-compatible CA bundle"
Write-Host "    SSL_CERT_FILE                  OpenSSL/requests CA bundle"
Write-Host ""
Write-Host "  Managed PreToolUse/PostToolUse matcher:"
Write-Host "    $ToolMatcher"
Write-Host ""
Write-Host "  To verify Stop hook execution:"
Write-Host "    Set F5_GUARDRAILS_LOG_LEVEL=debug for detailed scan context."
Write-Host "    Get-Content -Path `"$DefaultLogFile`" -Wait"
Write-Host "    Look for: context=assistant_response, hook=Stop, Scan [assistant_response]"
Write-Host ""
Write-Host "  Restart Codex GUI / CLI for hooks to take effect."
Write-Host ("=" * 72)
