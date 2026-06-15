"""
F5 AI Guardrails client for Codex hooks.

Wraps the F5 Scan API (/backend/v1/scans) with:
- Environment-variable-based configuration
- Configurable fail-open / fail-closed behavior
- Timeout handling and structured error responses
- Optional CA bundle support for TLS inspection tools such as Zscaler
- Optional client certificate support for mTLS
- Debug logging for TLS/env/request outcomes, with secret values redacted
- File logging under .codex/logs by default
- Structured logging to stderr and file only (never stdout — Codex reads stdout for decisions)
"""

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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
F5_DEBUG = os.getenv("F5_GUARDRAILS_DEBUG", "").lower() in ("1", "true", "yes", "on")
F5_DEBUG_REQUESTS = os.getenv("F5_GUARDRAILS_DEBUG_REQUESTS", "").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
F5_DEBUG_BODY_CHARS = int(os.getenv("F5_GUARDRAILS_DEBUG_BODY_CHARS", "500"))
F5_LOG_FILE = os.getenv("F5_GUARDRAILS_LOG_FILE", "").strip()

SCAN_ENDPOINT = f"{F5_BASE_URL.rstrip('/')}/backend/v1/scans"

# Log levels as integers for comparison
_LOG_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _default_log_file() -> Path | None:
    """
    Return the file used for mirrored hook logs.

    Defaults to:
      %CODEX_HOME%\\logs\\f5_guardrails.log
    or:
      %USERPROFILE%\\.codex\\logs\\f5_guardrails.log

    Set F5_GUARDRAILS_LOG_FILE to an explicit path to override this.
    Set F5_GUARDRAILS_LOG_FILE to "off", "false", "0", or "none" to disable file logging.
    """
    if F5_LOG_FILE.lower() in ("off", "false", "0", "none", "disabled"):
        return None

    if F5_LOG_FILE:
        return Path(F5_LOG_FILE).expanduser()

    codex_home = os.getenv("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(codex_home).expanduser() / "logs" / "f5_guardrails.log"


def _write_file_log(line: str) -> None:
    """Best-effort file logging. Never raise into the hook path."""
    log_file = _default_log_file()
    if log_file is None:
        return

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        # stderr is safe for diagnostics; stdout is reserved for Codex protocol.
        print(f"[f5-guardrails] [WARN] failed to write log file {log_file}: {exc}", file=sys.stderr)


def _log(level: str, msg: str) -> None:
    """Log to stderr and mirror to a file; stdout is reserved for Codex hook protocol."""
    threshold = 0 if (F5_DEBUG or F5_DEBUG_REQUESTS) else _LOG_LEVELS.get(F5_LOG_LEVEL, 2)
    if _LOG_LEVELS.get(level, 2) >= threshold:
        console_line = f"[f5-guardrails] [{level.upper()}] {msg}"
        print(console_line, file=sys.stderr)

        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        file_line = f"{timestamp} pid={os.getpid()} {console_line}"
        _write_file_log(file_line)


def _mask_value(name: str, value: str) -> str:
    """Return a log-safe representation of an environment variable value."""
    if not value:
        return "<unset>"

    sensitive_tokens = ("TOKEN", "SECRET", "PASSWORD", "PASS", "KEY")
    is_sensitive = any(token in name.upper() for token in sensitive_tokens)

    # Paths to key files are useful for troubleshooting and are not the key material itself.
    if name.upper().endswith("_CLIENT_KEY") or name.upper() in ("SSLKEYLOGFILE",):
        is_sensitive = False

    if not is_sensitive:
        return value

    if len(value) <= 10:
        return "<set:redacted>"

    return f"{value[:4]}...{value[-4:]} ({len(value)} chars)"


def _debug_env_snapshot() -> None:
    """Emit selected config and TLS/proxy env vars for troubleshooting."""
    if not (F5_DEBUG or F5_DEBUG_REQUESTS):
        return

    names = [
        "F5_GUARDRAILS_BASE_URL",
        "F5_GUARDRAILS_API_TOKEN",
        "F5_GUARDRAILS_PROJECT_ID",
        "F5_GUARDRAILS_TIMEOUT",
        "F5_GUARDRAILS_FAIL_MODE",
        "F5_GUARDRAILS_LOG_LEVEL",
        "F5_GUARDRAILS_LOG_FILE",
        "F5_GUARDRAILS_DEBUG",
        "F5_GUARDRAILS_DEBUG_REQUESTS",
        "F5_GUARDRAILS_DEBUG_BODY_CHARS",
        "F5_GUARDRAILS_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_DIR",
        "F5_GUARDRAILS_CLIENT_CERT",
        "F5_GUARDRAILS_CLIENT_KEY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
    ]

    _log("debug", "Environment snapshot begin")
    for name in names:
        value = os.getenv(name, "")
        _log("debug", f"env {name}={_mask_value(name, value)}")
    _log("debug", "Environment snapshot end")


def _response_preview(resp: requests.Response) -> str:
    """Return a short, single-line preview of a response body."""
    if F5_DEBUG_BODY_CHARS <= 0:
        return "<disabled>"

    text = resp.text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > F5_DEBUG_BODY_CHARS:
        return f"{text[:F5_DEBUG_BODY_CHARS]}...<truncated>"
    return text


def _env_file(name: str) -> str | None:
    """
    Return a validated file path from an environment variable.

    Empty variables are ignored. Missing files are logged and ignored so the
    hook can still honor the configured fail-open / fail-closed behavior when
    requests raises a TLS or connection error.
    """
    value = os.getenv(name, "").strip()
    if not value:
        return None

    path = Path(value).expanduser()
    if not path.is_file():
        _log("error", f"{name} points to a missing file: {path}")
        return None

    return str(path)


def _requests_tls_kwargs() -> dict[str, Any]:
    """
    Build TLS options for requests from environment variables.

    CA trust bundle, used for Zscaler or other TLS inspection roots:
      F5_GUARDRAILS_CA_BUNDLE
      REQUESTS_CA_BUNDLE
      SSL_CERT_FILE

    Optional mTLS client certificate:
      F5_GUARDRAILS_CLIENT_CERT
      F5_GUARDRAILS_CLIENT_KEY

    If F5_GUARDRAILS_CLIENT_KEY is omitted, F5_GUARDRAILS_CLIENT_CERT may point
    to a combined PEM containing both the client certificate and private key.
    """
    kwargs: dict[str, Any] = {}

    ca_bundle = (
        _env_file("F5_GUARDRAILS_CA_BUNDLE")
        or _env_file("REQUESTS_CA_BUNDLE")
        or _env_file("SSL_CERT_FILE")
    )
    if ca_bundle:
        kwargs["verify"] = ca_bundle
        _log("debug", f"Using CA bundle: {ca_bundle}")

    client_cert = _env_file("F5_GUARDRAILS_CLIENT_CERT")
    client_key = _env_file("F5_GUARDRAILS_CLIENT_KEY")

    if client_cert and client_key:
        kwargs["cert"] = (client_cert, client_key)
        _log("debug", "Using mTLS client certificate and key.")
    elif client_cert:
        kwargs["cert"] = client_cert
        _log("debug", "Using mTLS combined client certificate PEM.")
    elif client_key:
        _log(
            "warn",
            "F5_GUARDRAILS_CLIENT_KEY is set but F5_GUARDRAILS_CLIENT_CERT is not; ignoring client key.",
        )

    return kwargs


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
    _debug_env_snapshot()

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

    tls_kwargs = _requests_tls_kwargs()

    _log("debug", f"Scanning [{context}]: {len(text)} chars → {SCAN_ENDPOINT}")
    if F5_DEBUG_REQUESTS:
        verify_value = tls_kwargs.get("verify", "<requests default>")
        cert_value = tls_kwargs.get("cert", "<none>")
        _log(
            "debug",
            "requests.post config: "
            f"url={SCAN_ENDPOINT} timeout={F5_TIMEOUT}s verify={verify_value} cert={cert_value} "
            f"project_set={bool(F5_PROJECT_ID)} metadata_keys={sorted((metadata or {}).keys())}",
        )

    start = time.monotonic()
    try:
        resp = requests.post(
            SCAN_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=F5_TIMEOUT,
            **tls_kwargs,
        )
    except requests.Timeout:
        duration = (time.monotonic() - start) * 1000
        _log("error", f"Scan timed out after {F5_TIMEOUT}s for [{context}].")
        if F5_DEBUG_REQUESTS:
            _log("debug", f"requests outcome: timeout duration_ms={duration:.0f}")
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
        if F5_DEBUG_REQUESTS:
            _log(
                "debug",
                f"requests outcome: exception type={type(exc).__name__} duration_ms={duration:.0f}",
            )
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

    if F5_DEBUG_REQUESTS:
        _log(
            "debug",
            "requests outcome: "
            f"status_code={resp.status_code} ok={resp.ok} duration_ms={duration:.0f} "
            f"content_type={resp.headers.get('content-type', '<unset>')} "
            f"body_preview={_response_preview(resp)}",
        )

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

    if F5_DEBUG_REQUESTS:
        _log("debug", f"response JSON top-level keys: {sorted(data.keys())}")

    result = data.get("result", {})
    outcome = result.get("outcome", "error")
    scanner_results = result.get("scannerResults", [])

    if F5_DEBUG_REQUESTS:
        scanner_outcomes = [
            str(item.get("outcome", "unknown"))
            for item in scanner_results[:10]
            if isinstance(item, dict)
        ]
        _log(
            "debug",
            f"F5 scan outcome: outcome={outcome} scanner_count={len(scanner_results)} "
            f"scanner_outcomes={scanner_outcomes}",
        )

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
