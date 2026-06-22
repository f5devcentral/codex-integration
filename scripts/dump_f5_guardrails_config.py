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
Dump read-only F5 AI Security / Guardrails configuration for troubleshooting.

The script collects providers/connections, projects, API token metadata, and
project/provider relationships. It redacts secret-like fields before writing
the output.

Environment:
  F5_GLOBAL_API_TOKEN       preferred token env var for this diagnostic
  F5_GUARDRAILS_API_TOKEN   fallback
  CALYPSOAI_TOKEN           fallback used in F5 examples
  F5_GUARDRAILS_BASE_URL    optional, defaults to https://us1.calypsoai.app

Example:
  export F5_GLOBAL_API_TOKEN='...'
  python3 scripts/dump_f5_guardrails_config.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://us1.calypsoai.app"
DEFAULT_OUTPUT = "f5_guardrails_config_dump.redacted.json"

TOKEN_ENV_NAMES = (
    "F5_GLOBAL_API_TOKEN",
    "F5_GUARDRAILS_API_TOKEN",
    "CALYPSOAI_TOKEN",
)

SENSITIVE_KEY_PARTS = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "password",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "clientsecret",
    "client_secret",
)


def find_token() -> tuple[str, str]:
    for name in TOKEN_ENV_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return name, value
    raise SystemExit(
        "Missing API token. Set one of: "
        + ", ".join(TOKEN_ENV_NAMES)
    )


def redact(value: Any, key: str = "") -> Any:
    key_lower = key.lower()

    # Collection/diagnostic keys are safe to traverse. Individual fields inside
    # them, such as "token" or "secret", are still redacted below.
    if key_lower in ("tokens", "tokenenv"):
        if isinstance(value, dict):
            return {item_key: redact(item_value, item_key) for item_key, item_value in value.items()}
        if isinstance(value, list):
            return [redact(item, key) for item in value]
        return value

    if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
        if value in (None, "", [], {}):
            return value
        return "<redacted>"

    if isinstance(value, dict):
        return {item_key: redact(item_value, item_key) for item_key, item_value in value.items()}

    if isinstance(value, list):
        return [redact(item, key) for item in value]

    return value


def request_json(base_url: str, token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean_params:
            url = f"{url}?{urllib.parse.urlencode(clean_params)}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc

    if not body:
        return None

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GET {url} returned non-JSON response: {body[:500]}") from exc


def extract_items(response: Any, collection_key: str) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        value = response.get(collection_key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    return []


def next_cursor(response: Any) -> str | None:
    if not isinstance(response, dict):
        return None

    value = response.get("next")
    if not value:
        return None

    if isinstance(value, str):
        parsed = urllib.parse.urlparse(value)
        if parsed.query:
            query = urllib.parse.parse_qs(parsed.query)
            cursor_values = query.get("cursor")
            if cursor_values:
                return cursor_values[0]
        return value

    return None


def get_collection(
    base_url: str,
    token: str,
    path: str,
    collection_key: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    pages: list[Any] = []
    cursor = None
    seen_cursors: set[str] = set()

    while True:
        page_params = dict(params or {})
        page_params["limit"] = page_params.get("limit", 100)
        if cursor:
            page_params["cursor"] = cursor

        response = request_json(base_url, token, path, page_params)
        pages.append(response)
        items.extend(extract_items(response, collection_key))

        cursor = next_cursor(response)
        if not cursor or cursor in seen_cursors:
            break
        seen_cursors.add(cursor)

    return {
        "count": len(items),
        "items": items,
        "raw_pages": pages,
    }


def display_name(item: dict[str, Any]) -> str:
    for key in ("name", "displayName", "display_name", "tag", "id"):
        value = item.get(key)
        if value:
            return str(value)
    return "<unnamed>"


def build_summary(dump: dict[str, Any]) -> dict[str, Any]:
    providers = dump["providers"]["items"]
    projects = dump["projects"]["items"]
    tokens = dump["tokens"]["items"]
    relationships = dump["project_provider_relationships"]

    return {
        "providers": [
            {
                "id": provider.get("id"),
                "name": display_name(provider),
                "type": provider.get("type"),
                "tag": provider.get("tag"),
                "capabilities": provider.get("capabilities"),
                "projectId": provider.get("projectId"),
                "availability": provider.get("availability"),
                "systemProviderId": provider.get("systemProviderId"),
            }
            for provider in providers
        ],
        "projects": [
            {
                "id": project.get("id"),
                "name": display_name(project),
                "type": project.get("type"),
                "deploymentStatus": project.get("deploymentStatus"),
                "providerIds": project.get("providerIds"),
                "scannerPackageIds": project.get("scannerPackageIds"),
            }
            for project in projects
        ],
        "tokens": [
            {
                "id": token.get("id"),
                "name": display_name(token),
                "projectId": token.get("projectId"),
                "roleId": token.get("roleId"),
                "createdAt": token.get("createdAt"),
                "expiresAt": token.get("expiresAt"),
            }
            for token in tokens
        ],
        "project_provider_relationships": relationships,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("F5_GUARDRAILS_BASE_URL", DEFAULT_BASE_URL),
        help=f"F5 AI Security base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Optional project name/id to highlight. Can be provided more than once.",
    )
    args = parser.parse_args()

    token_env, token = find_token()
    base_url = args.base_url.rstrip("/")

    print(f"[INFO] Base URL: {base_url}")
    print(f"[INFO] Token env: {token_env}")
    print("[INFO] Fetching providers...")
    providers = get_collection(base_url, token, "/backend/v1/providers", "providers")

    print("[INFO] Fetching projects...")
    projects = get_collection(base_url, token, "/backend/v1/projects", "projects")

    print("[INFO] Fetching API token metadata...")
    tokens = get_collection(base_url, token, "/backend/v1/tokens", "tokens")

    relationships: list[dict[str, Any]] = []
    for project in projects["items"]:
        project_id = project.get("id")
        if not project_id:
            continue

        project_name = display_name(project)
        print(f"[INFO] Fetching provider relationships for project: {project_name} ({project_id})")

        relationship = {
            "project": {
                "id": project_id,
                "name": project_name,
                "type": project.get("type"),
                "deploymentStatus": project.get("deploymentStatus"),
            },
            "providers_added_to_project": get_collection(
                base_url,
                token,
                "/backend/v1/providers",
                "providers",
                {"addedToProjectId": project_id},
            )["items"],
            "providers_accessible_to_project": get_collection(
                base_url,
                token,
                "/backend/v1/providers",
                "providers",
                {"accessibleToProjectId": project_id},
            )["items"],
            "providers_enabled_for_project": get_collection(
                base_url,
                token,
                "/backend/v1/providers",
                "providers",
                {"enabledForProjectId": project_id},
            )["items"],
        }
        relationships.append(relationship)

    dump = {
        "generatedAtUnix": int(time.time()),
        "baseUrl": base_url,
        "tokenEnv": token_env,
        "highlightProjects": args.project,
        "providers": providers,
        "projects": projects,
        "tokens": tokens,
        "project_provider_relationships": relationships,
    }
    dump["summary"] = build_summary(dump)

    redacted_dump = redact(dump)
    output_path = Path(args.output).expanduser().resolve()
    output_path.write_text(json.dumps(redacted_dump, indent=2, sort_keys=True), encoding="utf-8")

    print(f"[OK] Wrote redacted config dump: {output_path}")
    print("[INFO] Quick counts:")
    print(f"  providers: {providers['count']}")
    print(f"  projects:  {projects['count']}")
    print(f"  tokens:    {tokens['count']}")

    if args.project:
        wanted = {value.lower() for value in args.project}
        print("[INFO] Highlighted project/provider status:")
        for relationship in redacted_dump["project_provider_relationships"]:
            project = relationship["project"]
            project_values = {
                str(project.get("id", "")).lower(),
                str(project.get("name", "")).lower(),
            }
            if not wanted.intersection(project_values):
                continue
            enabled = relationship["providers_enabled_for_project"]
            added = relationship["providers_added_to_project"]
            accessible = relationship["providers_accessible_to_project"]
            print(f"  project: {project.get('name')} ({project.get('id')})")
            print(f"    added providers:      {len(added)}")
            print(f"    accessible providers: {len(accessible)}")
            print(f"    enabled providers:    {len(enabled)}")
            for provider in enabled:
                print(f"      - {display_name(provider)} ({provider.get('id')})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
