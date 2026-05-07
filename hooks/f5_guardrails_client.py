"""
F5 AI Guardrails client for Codex hooks.

Wraps the F5 Scan API (/backend/v1/scans) with:
- Environment-variable-based configuration
- Configurable fail-open / fail-closed behavior
- Timeout handling and structured error responses
- Structured logging to stderr (never stdout — Codex reads stdout for decisions)
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
except ImportError:
    print(
        "ERROR: 'requests' package is required. Install with: pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration (all from environment variables)
# ---------------------------------------------------------------------------

F5_BASE_URL = os.getenv("F5_GUARDRAILS_BASE_URL", "https://www.us1.calypsoai.app")
F5_API_TOKEN = os.getenv("F5_GUARDRAILS_API_TOKEN", "")
F5_PROJECT_ID = os.getenv("F5_GUARDRAILS_PROJECT_ID", "")
F5_TIMEOUT = int(os.getenv("F5_GUARDRAILS_TIMEOUT", "10"))
F5_FAIL_MODE = os.getenv("F5_GUARDRAILS_FAIL_MODE", "open")  # "open" or "closed"
F5_LOG_LEVEL = os.getenv("F5_GUARDRAILS_LOG_LEVEL", "warn")  # "debug", "info", "warn", "error"

SCAN_ENDPOINT = f"{F5_BASE_URL.rstrip('/')}/backend/v1/scans"

# Log levels as integers for comparison
_LOG_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _log(level: str, msg: str) -> None:
    """Log to stderr only — stdout is reserved for Codex hook protocol."""
    threshold = _LOG_LEVELS.get(F5_LOG_LEVEL, 2)
    if _LOG_LEVELS.get(level, 2) >= threshold:
        print(f"[f5-guardrails] [{level.upper()}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    """Structured result from an F5 Guardrails scan."""

    outcome: str  # "cleared", "flagged", "blocked", or "error"
    message: str = ""
    scanner_results: list = field(default_factory=list)
    duration_ms: float = 0.0
    raw_response: Optional[dict] = None

    @property
    def is_blocked(self) -> bool:
        return self.outcome not in ("cleared", "passed")

    @property
    def is_error(self) -> bool:
        return self.outcome == "error"


# ---------------------------------------------------------------------------
# Core scan function
# ---------------------------------------------------------------------------

def scan(text: str, context: str = "", metadata: Optional[dict] = None) -> ScanResult:
    """
    Send text to F5 Guardrails Scan API and return a structured result.

    Args:
        text: The content to scan.
        context: A label for logging (e.g., "user_prompt", "bash_command").
        metadata: Optional external metadata dict sent to F5.

    Returns:
        ScanResult with outcome, message, and scanner details.
    """
    if not F5_API_TOKEN:
        _log("error", "F5_GUARDRAILS_API_TOKEN is not set — cannot scan.")
        if F5_FAIL_MODE == "closed":
            return ScanResult(
                outcome="error",
                message="F5 Guardrails API token not configured. Fail-closed: blocking.",
            )
        _log("warn", "Fail-open: allowing without scan.")
        return ScanResult(outcome="cleared", message="No API token — fail-open bypass.")

    if not text or not text.strip():
        _log("debug", f"Empty content for [{context}] — skipping scan.")
        return ScanResult(outcome="cleared", message="Empty content — nothing to scan.")

    headers = {
        "Authorization": f"Bearer {F5_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload: dict = {"input": text}
    if F5_PROJECT_ID:
        payload["project"] = F5_PROJECT_ID
    if metadata:
        payload["externalMetadata"] = metadata

    _log("debug", f"Scanning [{context}]: {len(text)} chars → {SCAN_ENDPOINT}")

    start = time.monotonic()
    try:
        resp = requests.post(
            SCAN_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=F5_TIMEOUT,
        )
    except requests.Timeout:
        duration = (time.monotonic() - start) * 1000
        _log("error", f"Scan timed out after {F5_TIMEOUT}s for [{context}].")
        if F5_FAIL_MODE == "closed":
            return ScanResult(
                outcome="error",
                message=f"F5 scan timed out ({F5_TIMEOUT}s). Fail-closed: blocking.",
                duration_ms=duration,
            )
        return ScanResult(
            outcome="cleared",
            message=f"F5 scan timed out ({F5_TIMEOUT}s). Fail-open: allowing.",
            duration_ms=duration,
        )
    except requests.RequestException as exc:
        duration = (time.monotonic() - start) * 1000
        _log("error", f"Scan request failed for [{context}]: {exc}")
        if F5_FAIL_MODE == "closed":
            return ScanResult(
                outcome="error",
                message=f"F5 scan failed: {exc}. Fail-closed: blocking.",
                duration_ms=duration,
            )
        return ScanResult(
            outcome="cleared",
            message=f"F5 scan failed: {exc}. Fail-open: allowing.",
            duration_ms=duration,
        )

    duration = (time.monotonic() - start) * 1000

    if resp.status_code != 200:
        _log("error", f"Scan returned HTTP {resp.status_code} for [{context}]: {resp.text[:200]}")
        if F5_FAIL_MODE == "closed":
            return ScanResult(
                outcome="error",
                message=f"F5 returned HTTP {resp.status_code}. Fail-closed: blocking.",
                duration_ms=duration,
            )
        return ScanResult(
            outcome="cleared",
            message=f"F5 returned HTTP {resp.status_code}. Fail-open: allowing.",
            duration_ms=duration,
        )

    try:
        data = resp.json()
    except ValueError:
        _log("error", f"Non-JSON response from F5 for [{context}].")
        if F5_FAIL_MODE == "closed":
            return ScanResult(
                outcome="error",
                message="F5 returned invalid JSON. Fail-closed: blocking.",
                duration_ms=duration,
            )
        return ScanResult(
            outcome="cleared",
            message="F5 returned invalid JSON. Fail-open: allowing.",
            duration_ms=duration,
        )

    result = data.get("result", {})
    outcome = result.get("outcome", "error")
    scanner_results = result.get("scannerResults", [])

    _log(
        "info",
        f"Scan [{context}] → {outcome} ({len(scanner_results)} scanners, {duration:.0f}ms)",
    )

    return ScanResult(
        outcome=outcome,
        message=f"F5 Guardrails: {outcome}",
        scanner_results=scanner_results,
        duration_ms=duration,
        raw_response=data,
    )


# ---------------------------------------------------------------------------
# Hook I/O helpers
# ---------------------------------------------------------------------------

def read_hook_input() -> dict:
    """Read the JSON payload Codex sends on stdin."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, IOError) as exc:
        _log("error", f"Failed to read hook input: {exc}")
        return {}


def emit_json(data: dict) -> None:
    """Write a JSON response to stdout for Codex to consume."""
    print(json.dumps(data, separators=(",", ":")))


def emit_block(reason: str, feedback: str = "") -> None:
    """Emit a block decision (PreToolUse / PermissionRequest)."""
    output: dict = {"decision": "block", "reason": reason}
    if feedback:
        output["systemMessage"] = feedback
    emit_json(output)


def emit_stop(reason: str, message: str = "") -> None:
    """Emit a stop decision (UserPromptSubmit / Stop)."""
    output: dict = {"continue": False, "stopReason": reason}
    if message:
        output["systemMessage"] = message
    emit_json(output)


def emit_warn(message: str) -> None:
    """Emit a warning that surfaces in the UI but doesn't block."""
    emit_json({"systemMessage": message})
