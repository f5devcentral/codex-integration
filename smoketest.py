"""
.SYNOPSIS
    Codex <-> F5 AI Guardrails - Smoke Test

.DESCRIPTION
    Validates that the F5 AI Guardrails Python client can be imported from the
    Codex hooks directory.

    Resolves the hooks path from CODEX_HOME if set, otherwise defaults to:

        %USERPROFILE%\\.codex\\hooks\\f5_guardrails\\

    Checks whether F5_GUARDRAILS_API_TOKEN is present in the calling shell
    environment.

    Optionally performs TLS diagnostics against the Guardrails API endpoint.

    Optionally enables verbose HTTP debug logging for requests / urllib3.

    Runs a smoke test scan using the prompt:

        "Hello, this is a test prompt."

    Prints the scan outcome, duration, and any error message returned by the
    Guardrails client.

.NOTES
    File:
        smoketest.py

    Expected client module:
        f5_guardrails_client.py

    Expected environment variable:
        F5_GUARDRAILS_API_TOKEN

    Optional environment variables:
        CODEX_HOME
        F5_GUARDRAILS_USE_SYSTEM_CERT_STORE
        REQUESTS_CA_BUNDLE
        SSL_CERT_FILE
        CURL_CA_BUNDLE
        SSLKEYLOGFILE

.EXAMPLES
    Basic smoke test:

        python .\\smoketest.py

    Smoke test with TLS diagnostics:

        python .\\smoketest.py --tls-diagnostics

    Smoke test with verbose HTTP request/response debugging:

        python .\\smoketest.py --verbose-http

    Full troubleshooting mode:

        python .\\smoketest.py --tls-diagnostics --verbose-http

.WARNING
    Verbose HTTP debugging may expose sensitive headers, including API tokens.
    Redact secrets before sharing logs.
"""

from __future__ import annotations

import argparse
import http.client as http_client
import logging
import os
import socket
import ssl
import sys
import tempfile
from pathlib import Path
from typing import Optional


try:
    sys.stdout.reconfigure(line_buffering=True)
except AttributeError:
    pass


def env_switch(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().lower()


def truthy(value: str) -> bool:
    return value in ("1", "true", "yes", "on")


def falsey(value: str) -> bool:
    return value in ("0", "false", "no", "off", "none", "disabled")


def system_cert_store_requested() -> bool:
    value = env_switch("F5_GUARDRAILS_USE_SYSTEM_CERT_STORE", "auto")
    if truthy(value):
        return True
    if falsey(value):
        return False
    return os.name == "nt"


TRUSTSTORE_STATUS = "disabled"
TRUSTSTORE_MODULE = None
if system_cert_store_requested():
    try:
        import truststore

        TRUSTSTORE_MODULE = truststore
        TRUSTSTORE_STATUS = "enabled via truststore"
    except ImportError:
        TRUSTSTORE_STATUS = "unavailable: truststore is not installed"
    except Exception as exc:
        TRUSTSTORE_STATUS = f"unavailable: {type(exc).__name__}: {exc}"


DEFAULT_API_HOST = "www.us1.calypsoai.app"
DEFAULT_API_PORT = 443
SMOKE_TEST_PROMPT = "Hello, this is a test prompt."
SMOKE_TEST_CONTEXT = "smoke_test"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def write_ok(message: str) -> None:
    print(f"[OK] {message}")


def write_info(message: str) -> None:
    print(f"[INFO] {message}")


def write_warn(message: str) -> None:
    print(f"[WARN] {message}")


def write_error(message: str) -> None:
    print(f"[ERROR] {message}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def get_codex_home() -> Path:
    """
    Equivalent to the PowerShell logic:

        if ($env:CODEX_HOME) {
            $env:CODEX_HOME
        } else {
            Join-Path $env:USERPROFILE ".codex"
        }
    """

    codex_home = os.environ.get("CODEX_HOME")

    if codex_home:
        return Path(codex_home).expanduser().resolve()

    user_profile = os.environ.get("USERPROFILE")

    if user_profile:
        return (Path(user_profile) / ".codex").expanduser().resolve()

    raise RuntimeError("Neither CODEX_HOME nor USERPROFILE is set.")


def get_hooks_dir() -> Path:
    return get_codex_home() / "hooks" / "f5_guardrails"


def add_hooks_dir_to_python_path(hooks_dir: Path) -> None:
    if not hooks_dir.exists():
        raise FileNotFoundError(f"Hooks directory not found: {hooks_dir}")

    if not hooks_dir.is_dir():
        raise NotADirectoryError(f"Hooks path exists but is not a directory: {hooks_dir}")

    hooks_dir_str = str(hooks_dir)

    if hooks_dir_str not in sys.path:
        sys.path.insert(0, hooks_dir_str)


# ---------------------------------------------------------------------------
# Verbose HTTP debug
# ---------------------------------------------------------------------------

def enable_http_debug() -> None:
    """
    Enables HTTP-level debug logging for http.client / urllib3 / requests.

    This may print:
      - request method/path
      - headers
      - response status
      - response headers
      - send/receive traces

    WARNING:
      Authorization headers may be printed.
    """

    print()
    write_warn("Verbose HTTP debug logging is enabled.")
    write_warn("This may expose Authorization headers or API tokens in the console output.")

    http_client.HTTPConnection.debuglevel = 1

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    logging.getLogger("urllib3").setLevel(logging.DEBUG)
    logging.getLogger("requests").setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# TLS diagnostics
# ---------------------------------------------------------------------------

def print_python_tls_environment() -> None:
    print()
    print("Python / TLS environment:")
    print(f"  Python executable:       {sys.executable}")
    print(f"  Python version:          {sys.version}")
    print(f"  OpenSSL version:         {ssl.OPENSSL_VERSION}")
    print(f"  System cert store:       {TRUSTSTORE_STATUS}")
    print(f"  F5 use system store:     {os.environ.get('F5_GUARDRAILS_USE_SYSTEM_CERT_STORE', 'auto')}")
    print(f"  SSL_CERT_FILE:           {os.environ.get('SSL_CERT_FILE')}")
    print(f"  REQUESTS_CA_BUNDLE:      {os.environ.get('REQUESTS_CA_BUNDLE')}")
    print(f"  CURL_CA_BUNDLE:          {os.environ.get('CURL_CA_BUNDLE')}")
    print(f"  SSLKEYLOGFILE:           {os.environ.get('SSLKEYLOGFILE')}")

    try:
        import certifi  # type: ignore

        print(f"  certifi CA bundle:       {certifi.where()}")
    except Exception as exc:
        print(f"  certifi CA bundle:       unavailable: {type(exc).__name__}: {exc}")


def create_verified_tls_context() -> ssl.SSLContext:
    if TRUSTSTORE_MODULE is not None:
        return TRUSTSTORE_MODULE.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    return ssl.create_default_context()


def decode_der_certificate_for_display(cert_der: bytes) -> Optional[dict]:
    """
    Best-effort helper to decode a DER cert using Python's internal cert parser.

    This avoids adding a cryptography dependency just for diagnostics.

    Returns:
        dict if decoding works, otherwise None.
    """

    if not cert_der:
        return None

    try:
        cert_pem = ssl.DER_cert_to_PEM_cert(cert_der)

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pem",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            tmp.write(cert_pem)
            tmp_path = tmp.name

        try:
            return ssl._ssl._test_decode_cert(tmp_path)  # type: ignore[attr-defined]
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except Exception:
        return None


def format_cert_name(name_value) -> str:
    """
    Converts certificate subject / issuer tuple structure into a readable string.
    """

    if not name_value:
        return "unavailable"

    try:
        parts = []

        for rdn in name_value:
            for key, value in rdn:
                parts.append(f"{key}={value}")

        return ", ".join(parts) if parts else str(name_value)

    except Exception:
        return str(name_value)


def attempt_verified_tls_handshake(host: str, port: int) -> bool:
    print()
    print("Verified TLS handshake:")
    print(f"  Target:                  {host}:{port}")

    try:
        context = create_verified_tls_context()

        with socket.create_connection((host, port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                print("  Result:                  OK")
                print(f"  TLS version:             {tls.version()}")
                print(f"  Cipher:                  {tls.cipher()}")

                cert = tls.getpeercert()
                print(f"  Subject:                 {format_cert_name(cert.get('subject'))}")
                print(f"  Issuer:                  {format_cert_name(cert.get('issuer'))}")
                print(f"  Valid from:              {cert.get('notBefore')}")
                print(f"  Valid until:             {cert.get('notAfter')}")

                return True

    except Exception as exc:
        print("  Result:                  FAILED")
        print(f"  Error type:              {type(exc).__name__}")
        print(f"  Error:                   {exc}")
        return False


def attempt_unverified_tls_handshake(host: str, port: int) -> None:
    print()
    print("Unverified TLS handshake:")
    print("  WARNING:                 Diagnostic only. Do not use unverified TLS for scans.")
    print(f"  Target:                  {host}:{port}")

    try:
        context = ssl._create_unverified_context()

        with socket.create_connection((host, port), timeout=10) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                print("  Result:                  OK")
                print(f"  TLS version:             {tls.version()}")
                print(f"  Cipher:                  {tls.cipher()}")

                cert_der = tls.getpeercert(binary_form=True)
                decoded = decode_der_certificate_for_display(cert_der)

                if decoded:
                    print(f"  Subject:                 {format_cert_name(decoded.get('subject'))}")
                    print(f"  Issuer:                  {format_cert_name(decoded.get('issuer'))}")
                    print(f"  Valid from:              {decoded.get('notBefore')}")
                    print(f"  Valid until:             {decoded.get('notAfter')}")
                else:
                    print("  Peer certificate:        received, but could not decode")
                    print(f"  Peer cert bytes:         {len(cert_der) if cert_der else 0}")

    except Exception as exc:
        print("  Result:                  FAILED")
        print(f"  Error type:              {type(exc).__name__}")
        print(f"  Error:                   {exc}")


def print_tls_diagnostics(host: str, port: int) -> None:
    print_python_tls_environment()

    verified_ok = attempt_verified_tls_handshake(host, port)

    if not verified_ok:
        attempt_unverified_tls_handshake(host, port)

        print()
        write_warn("Verified TLS failed but unverified TLS may still connect.")
        write_warn("This usually points to a certificate trust or issuer-chain problem.")
        write_warn("Possible causes include TLS inspection, missing corporate root CA,")
        write_warn("an incomplete server certificate chain, or an outdated certifi bundle.")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def run_smoke_test(
    verbose_http: bool = False,
    tls_diagnostics: bool = False,
    tls_host: str = DEFAULT_API_HOST,
    tls_port: int = DEFAULT_API_PORT,
) -> bool:
    print()
    print("Running smoke test...")

    if not os.environ.get("F5_GUARDRAILS_API_TOKEN"):
        write_warn("Skipping smoke test - F5_GUARDRAILS_API_TOKEN is not set.")
        return False

    try:
        codex_home = get_codex_home()
        hooks_dir = get_hooks_dir()

        print()
        print("Resolved paths:")
        print(f"  CODEX_HOME:              {codex_home}")
        print(f"  Hooks directory:         {hooks_dir}")

        add_hooks_dir_to_python_path(hooks_dir)
        write_ok("Hooks directory added to Python import path.")

    except Exception as exc:
        write_error(f"Unable to prepare Python import path: {type(exc).__name__}: {exc}")
        return False

    if tls_diagnostics:
        print_tls_diagnostics(tls_host, tls_port)

    if verbose_http:
        enable_http_debug()

    try:
        from f5_guardrails_client import scan

    except Exception as exc:
        write_error(f"Unable to import f5_guardrails_client: {type(exc).__name__}: {exc}")
        return False

    try:
        print()
        print("Sending scan request:")
        print(f"  Prompt:                  {SMOKE_TEST_PROMPT}")
        print(f"  Context:                 {SMOKE_TEST_CONTEXT}")

        result = scan(
            SMOKE_TEST_PROMPT,
            context=SMOKE_TEST_CONTEXT,
        )

        print()
        print("Smoke test response:")
        print(f"  Outcome:                 {getattr(result, 'outcome', None)}")
        print(f"  Duration:                {getattr(result, 'duration_ms', 0):.0f}ms")
        print(f"  Is error:                {getattr(result, 'is_error', None)}")

        message = getattr(result, "message", None)
        if message:
            print(f"  Message:                 {message}")

        outcome = str(getattr(result, "outcome", "")).lower()
        is_error = bool(getattr(result, "is_error", False))

        # Important:
        # Do not treat "cleared" as success if result.is_error is True.
        # Some clients may fail open and return outcome=cleared with an error message.
        if is_error:
            write_error("Smoke test failed - scan returned an error.")
            return False

        if outcome in ("cleared", "passed"):
            write_ok(f"Smoke test passed: {outcome} ({getattr(result, 'duration_ms', 0):.0f}ms)")
            return True

        write_warn(f"Smoke test returned unexpected outcome: {outcome}")
        return False

    except Exception as exc:
        write_error("Smoke test failed - check API token, network connectivity, and TLS trust.")
        write_error(f"Details: {type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Codex <-> F5 AI Guardrails smoke test",
    )

    parser.add_argument(
        "--verbose-http",
        action="store_true",
        help="Enable verbose HTTP request/response debugging. May expose secrets.",
    )

    parser.add_argument(
        "--tls-diagnostics",
        action="store_true",
        help="Run TLS handshake diagnostics before the smoke test.",
    )

    parser.add_argument(
        "--tls-host",
        default=DEFAULT_API_HOST,
        help=f"TLS diagnostic host. Default: {DEFAULT_API_HOST}",
    )

    parser.add_argument(
        "--tls-port",
        type=int,
        default=DEFAULT_API_PORT,
        help=f"TLS diagnostic port. Default: {DEFAULT_API_PORT}",
    )

    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0, even if the smoke test fails.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    success = run_smoke_test(
        verbose_http=args.verbose_http,
        tls_diagnostics=args.tls_diagnostics,
        tls_host=args.tls_host,
        tls_port=args.tls_port,
    )

    if success:
        return 0

    if args.no_fail:
        write_warn("Smoke test failed, but --no-fail was specified. Exiting 0.")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
