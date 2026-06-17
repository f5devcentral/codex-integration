#!/usr/bin/env python3
"""
Codex hook: PreToolUse → F5 AI Guardrails

Scans tool inputs (Bash commands, file patches) through F5's Scan API
before the tool executes. Blocks execution if the scan flags the content.

Hook event: PreToolUse
Matcher:    Bash|apply_patch

Codex protocol:
  stdin:  JSON with tool_name, tool_input, session_id, turn_id, etc.
  stdout: JSON with {decision: "block", systemMessage} to block,
          or empty/exit 0 to allow.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from f5_guardrails_client import read_hook_input, scan, emit_block, _log, log_hook_entry


def _extract_scannable_text(hook_input: dict) -> tuple[str, str]:
    """
    Extract the text to scan from the hook input based on tool type.

    Returns:
        (text_to_scan, context_label)
    """
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool_name == "Bash":
        # Bash tool_input has a "command" field with the shell command string.
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "")
        elif isinstance(tool_input, str):
            command = tool_input
        else:
            command = str(tool_input)
        return command, "bash_command"

    if tool_name == "apply_patch":
        # apply_patch tool_input has a "patch" or "diff" field with the patch content.
        if isinstance(tool_input, dict):
            patch = tool_input.get("patch", tool_input.get("diff", ""))
            if not patch:
                # Fall back to the full tool_input as a string.
                patch = json.dumps(tool_input)
        elif isinstance(tool_input, str):
            patch = tool_input
        else:
            patch = str(tool_input)
        return patch, "file_patch"

    # Unknown tool — scan whatever we have.
    if isinstance(tool_input, dict):
        text = json.dumps(tool_input)
    elif isinstance(tool_input, str):
        text = tool_input
    else:
        text = str(tool_input)
    return text, f"tool_{tool_name}"


def main() -> None:
    log_hook_entry("pre_tool_use.py")

    hook_input = read_hook_input()

    tool_name = hook_input.get("tool_name", "unknown")
    text, context = _extract_scannable_text(hook_input)

    if not text or not text.strip():
        _log("debug", f"Empty tool input for {tool_name} — allowing.")
        return

    session_id = hook_input.get("session_id", "unknown")
    turn_id = hook_input.get("turn_id", "unknown")

    result = scan(
        text=text,
        context=context,
        metadata={
            "hook": "PreToolUse",
            "tool_name": tool_name,
            "session_id": session_id,
            "turn_id": turn_id,
        },
    )

    if result.is_error and result.outcome == "error":
        if result.is_blocked:
            emit_block(
                reason=f"F5 Guardrails error: {result.message}",
                feedback=f"F5 Guardrails could not complete the tool-input scan: {result.message}",
            )
        return

    if result.is_blocked:
        _log("warn", f"Tool input blocked by F5 Guardrails: {tool_name} → {result.outcome}")

        # Build a concise feedback message for the agent.
        triggered = [
            s for s in result.scanner_results if s.get("outcome") != "passed"
        ]
        scanner_info = ""
        if triggered:
            scanner_names = [s.get("scannerId", "unknown")[:12] for s in triggered[:3]]
            scanner_info = f" (scanners: {', '.join(scanner_names)})"

        emit_block(
            reason=f"F5 Guardrails blocked this {tool_name} call ({result.outcome})",
            feedback=(
                f"F5 Guardrails blocked this {tool_name} execution.\n"
                f"Outcome: {result.outcome}{scanner_info}\n"
                f"Try rephrasing or using a different approach."
            ),
        )
        return

    _log("debug", f"Tool input cleared: {tool_name} ({result.duration_ms:.0f}ms).")


if __name__ == "__main__":
    main()
