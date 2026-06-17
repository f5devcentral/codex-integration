#!/usr/bin/env python3
"""
Codex hook: UserPromptSubmit → F5 AI Guardrails

Scans every user prompt through F5's Scan API before it reaches the model.
If the scan returns anything other than "cleared", the prompt is blocked.

Hook event: UserPromptSubmit
Matcher:    (none — fires for all user prompts)

Codex protocol:
  stdin:  JSON with user_prompt, session_id, etc.
  stdout: JSON with {continue, stopReason, systemMessage} to block,
          or empty/exit 0 to allow.
"""

import os
import sys

# Ensure the hooks directory is on the path so we can import the client.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from f5_guardrails_client import read_hook_input, scan, emit_stop, _log, log_hook_entry


def main() -> None:
    log_hook_entry("user_prompt_submit.py")

    hook_input = read_hook_input()

    # Extract the user's prompt text.
    # Codex sends the field as "prompt", not "user_prompt".
    user_prompt = hook_input.get("prompt", hook_input.get("user_prompt", ""))
    if not user_prompt:
        _log("debug", "No prompt in hook input — allowing.")
        return

    session_id = hook_input.get("session_id", "unknown")

    result = scan(
        text=user_prompt,
        context="user_prompt",
        metadata={"hook": "UserPromptSubmit", "session_id": session_id},
    )

    if result.is_error and result.outcome == "error":
        # Error path — already handled by fail-open/closed in the client.
        if result.is_blocked:
            emit_stop(
                reason=f"F5 Guardrails error: {result.message}",
                message=f"⚠️ {result.message}",
            )
        # If fail-open cleared it, we just return (allow).
        return

    if result.is_blocked:
        _log("warn", f"Prompt blocked by F5 Guardrails: {result.outcome}")
        emit_stop(
            reason=f"F5 Guardrails blocked this prompt ({result.outcome})",
            message=(
                f"🛡️ F5 Guardrails blocked your prompt.\n"
                f"Outcome: {result.outcome}\n"
                f"Scanners triggered: {len([s for s in result.scanner_results if s.get('outcome') != 'passed'])}"
            ),
        )
        return

    # Cleared — exit 0 with no output.
    _log("debug", f"Prompt cleared ({result.duration_ms:.0f}ms).")


if __name__ == "__main__":
    main()
