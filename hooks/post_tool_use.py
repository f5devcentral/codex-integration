#!/usr/bin/env python3
"""
Codex hook: PostToolUse → F5 AI Guardrails

Scans tool outputs (stdout/stderr from Bash, patch results) through F5's
Scan API after the tool executes. Warns or blocks based on scan outcome.

By default, operates in audit mode (warn only). Set F5_GUARDRAILS_POST_STRICT=true
to block on flagged output (stops the current turn).

Hook event: PostToolUse
Matcher:    Bash|apply_patch

Codex protocol:
  stdin:  JSON with tool_name, tool_output, metadata, etc.
  stdout: JSON with {systemMessage} to warn,
          or {continue: false, stopReason, systemMessage} to stop the turn,
          or empty/exit 0 to allow.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from f5_guardrails_client import read_hook_input, scan, emit_warn, emit_json, _log

# Strict mode blocks on flagged output; default is audit-only (warn).
STRICT_MODE = os.getenv("F5_GUARDRAILS_POST_STRICT", "false").lower() in ("true", "1", "yes")

# Maximum output length to scan (avoid sending huge outputs to F5).
MAX_SCAN_LENGTH = int(os.getenv("F5_GUARDRAILS_MAX_SCAN_LENGTH", "50000"))


def _extract_output_text(hook_input: dict) -> tuple[str, str]:
    """
    Extract the tool output text from the hook input.

    Returns:
        (text_to_scan, context_label)
    """
    tool_name = hook_input.get("tool_name", "")
    tool_output = hook_input.get("tool_output", {})

    if isinstance(tool_output, dict):
        # Bash outputs typically have stdout and stderr.
        stdout = tool_output.get("stdout", "")
        stderr = tool_output.get("stderr", "")
        output_text = stdout
        if stderr:
            output_text = f"{output_text}\n--- stderr ---\n{stderr}" if output_text else stderr

        # apply_patch might have a "result" or "output" field.
        if not output_text:
            output_text = tool_output.get("result", tool_output.get("output", ""))

        if not output_text:
            output_text = json.dumps(tool_output)
    elif isinstance(tool_output, str):
        output_text = tool_output
    else:
        output_text = str(tool_output)

    context = f"output_{tool_name}" if tool_name else "tool_output"
    return output_text, context


def main() -> None:
    hook_input = read_hook_input()

    tool_name = hook_input.get("tool_name", "unknown")
    text, context = _extract_output_text(hook_input)

    if not text or not text.strip():
        _log("debug", f"Empty tool output for {tool_name} — skipping scan.")
        return

    # Truncate very large outputs to avoid slow/expensive scans.
    if len(text) > MAX_SCAN_LENGTH:
        _log("info", f"Truncating output from {len(text)} to {MAX_SCAN_LENGTH} chars for scan.")
        text = text[:MAX_SCAN_LENGTH]

    session_id = hook_input.get("session_id", "unknown")
    turn_id = hook_input.get("turn_id", "unknown")

    result = scan(
        text=text,
        context=context,
        metadata={
            "hook": "PostToolUse",
            "tool_name": tool_name,
            "session_id": session_id,
            "turn_id": turn_id,
        },
    )

    if result.is_error:
        # Errors in post-scan are less critical — log and continue.
        _log("warn", f"Post-scan error for {tool_name}: {result.message}")
        return

    if result.is_blocked:
        triggered = [
            s for s in result.scanner_results if s.get("outcome") != "passed"
        ]
        scanner_count = len(triggered)

        _log("warn", f"Tool output flagged by F5 Guardrails: {tool_name} → {result.outcome}")

        warning_msg = (
            f"🛡️ F5 Guardrails flagged output from {tool_name}.\n"
            f"Outcome: {result.outcome} | Scanners triggered: {scanner_count}"
        )

        if STRICT_MODE:
            _log("warn", "Strict mode: stopping turn due to flagged output.")
            emit_json({
                "continue": False,
                "stopReason": f"F5 Guardrails flagged {tool_name} output ({result.outcome})",
                "systemMessage": warning_msg,
            })
        else:
            # Audit mode: warn but don't block.
            emit_warn(warning_msg)
        return

    _log("debug", f"Tool output cleared: {tool_name} ({result.duration_ms:.0f}ms).")


if __name__ == "__main__":
    main()
