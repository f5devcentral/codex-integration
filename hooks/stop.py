#!/usr/bin/env python3
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

"""
Codex hook: Stop -> F5 AI Guardrails

Scans the final assistant response before it is shown to the user. This is
best-effort local/client-side response scanning using the Stop hook's
last_assistant_message field; it is not upstream model proxy enforcement.

Hook event: Stop
Matcher:    (none)

Codex protocol:
  stdin:  JSON with last_assistant_message, session_id, turn_id, etc.
  stdout: JSON with {continue: false, suppressOutput: true, stopReason,
          systemMessage} to block/suppress, or empty/exit 0 to allow.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from f5_guardrails_client import read_hook_input, scan, emit_json, _log, log_hook_entry


def _extract_last_assistant_message(hook_input: dict) -> str:
    message = hook_input.get("last_assistant_message", "")

    if isinstance(message, str):
        return message

    if message is None:
        return ""

    try:
        return json.dumps(message, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(message)


def _emit_response_block(outcome: str) -> None:
    reason = f"F5 Guardrails blocked assistant response ({outcome})"
    message = (
        "F5 Guardrails flagged the assistant response. "
        "Try rephrasing your request or ask for a safer summary."
    )

    emit_json(
        {
            "continue": False,
            "suppressOutput": True,
            "stopReason": reason,
            "systemMessage": message,
        }
    )


def main() -> None:
    log_hook_entry("stop.py")

    hook_input = read_hook_input()
    assistant_response = _extract_last_assistant_message(hook_input)

    if not assistant_response or not assistant_response.strip():
        _log("debug", "No last_assistant_message in Stop hook input - allowing.")
        return

    session_id = hook_input.get("session_id", "unknown")
    turn_id = hook_input.get("turn_id", "unknown")

    _log("debug", "Stop hook scanning context=assistant_response hook=Stop")

    result = scan(
        text=assistant_response,
        context="assistant_response",
        metadata={
            "hook": "Stop",
            "session_id": session_id,
            "turn_id": turn_id,
        },
    )

    if result.is_error:
        if result.is_blocked:
            _log("warn", f"Assistant response blocked by F5 Guardrails error: {result.message}")
            _emit_response_block(result.outcome)
        return

    if result.is_blocked:
        _log("warn", f"Assistant response blocked by F5 Guardrails: {result.outcome}")
        _emit_response_block(result.outcome)
        return

    _log("debug", f"Assistant response cleared ({result.duration_ms:.0f}ms).")


if __name__ == "__main__":
    main()
