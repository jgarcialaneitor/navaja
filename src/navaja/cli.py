"""CLI for fetching the full text of a CENDOJ resolution.

Usage::

    python -m navaja.cli <document_url>
    navaja-doc <document_url>
"""

from __future__ import annotations

import argparse
import socket
import sys

import httpx

from navaja.captcha import resolve_captcha_host, resolve_captcha_token
from navaja.cendoj import CendojClient
from navaja.documents import (
    FullTextResult,
    PdfSaveResult,
    parse_document_url,
    save_full_text_pdf,
)


# Seconds allowed for the pre-flight TCP connect probe.
_PROBE_TIMEOUT = 0.5

# Seconds allowed for the best-effort "is this navaja?" HTTP confirmation.
_CONFIRM_TIMEOUT = 1.0

# Marker present in every page the captcha listener serves (form and idle).
_NAVAJA_PAGE_MARKER = "<title>CENDOJ captcha</title>"


def _address_occupied(host: str, port: int) -> bool:
    """Return whether something is already listening on ``(host, port)``.

    Uses a connect probe rather than a trial bind: a trial bind would be
    vulnerable to ``TIME_WAIT`` and would race with the real bind performed
    later by the captcha server.
    """
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _navaja_listener_responds(host: str, port: int) -> bool:
    """Return whether the occupant of ``(host, port)`` is a navaja listener.

    Best effort: the captcha form URL for the resolved token is requested and
    the body is checked for navaja's own page. Any error, timeout or
    unexpected body answers ``False`` so the caller falls back to the generic
    message. The token is never echoed.
    """
    try:
        token, _reason = resolve_captcha_token()
        response = httpx.get(
            f"http://{host}:{port}/{token}/",
            timeout=_CONFIRM_TIMEOUT,
        )
    except Exception:
        return False
    return response.status_code == 200 and _NAVAJA_PAGE_MARKER in response.text


def _port_conflict_message(host: str, port: int) -> str:
    """Describe an occupied captcha port and the way forward.

    The occupant is confirmed before the message claims it is navaja, so the
    text states what is happening instead of guessing.
    """
    if _navaja_listener_responds(host, port):
        situation = (
            f"a navaja session is already holding {host} port {port}; "
            "its captcha form is the one answering there"
        )
    else:
        situation = f"{host} port {port} is in use by another process"
    return (
        f"Error: {situation}.\n"
        "Run with --port 0 to let the OS pick a free port (the form URL is "
        "announced on stderr), or stop the process holding the port."
    )


def _is_bind_conflict(exc: Exception) -> bool:
    """Return whether ``exc`` is the captcha server's address-in-use error."""
    message = str(exc)
    return "captcha server cannot bind to" in message and (
        "address already in use" in message
    )


def _preview(text: str, limit: int = 500) -> str:
    snippet = text[:limit]
    return snippet.replace("\n", " ")


def _report(
    result: FullTextResult,
    out_path: str | None,
    pdf_save_result: PdfSaveResult,
) -> None:
    status = "success" if result.ok else "failure"
    print(f"Result: {status}")
    print(f"Attempts: {result.attempts}")
    print(f"Content-Type: {result.content_type}")
    print(f"Text preview: {_preview(result.text)}")
    if result.error:
        print(f"Error: {result.error}")

    if out_path is not None:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(result.text)
        print(f"Wrote {len(result.text)} characters to {out_path}")

    if pdf_save_result.path is not None:
        print(f"PDF: {pdf_save_result.path} ({pdf_save_result.reason})")
    elif pdf_save_result.reason in ("not_pdf", "not_attempted"):
        print(f"PDF: not saved ({pdf_save_result.reason})")
    else:
        print(
            f"PDF: not saved ({pdf_save_result.reason}): {pdf_save_result.error}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the full text of a CENDOJ resolution",
    )
    parser.add_argument("url", help="CENDOJ document URL")
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "interface for the local captcha form "
            "(default: auto-detect: NAVAJA_CAPTCHA_HOST, then tailscale0 IPv4, then 127.0.0.1)"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port for the local captcha form (default: 8765)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds to wait for a captcha answer (default: 300)",
    )
    parser.add_argument(
        "--out",
        help="write extracted text to this file",
    )
    args = parser.parse_args(argv)

    if args.host is None:
        host, host_reason = resolve_captcha_host()
        print(
            f"Captcha form will bind to {host} because {host_reason}",
            file=sys.stderr,
        )
    else:
        host = args.host

    # Fail before any CENDOJ traffic: with a long-lived listener, an MCP
    # session holds the port for its whole life, and the human should not pay
    # a round-trip to a third-party site to learn that a local port is busy.
    # ``--port 0`` cannot collide, so it is not probed at all.
    if args.port != 0 and _address_occupied(host, args.port):
        print(_port_conflict_message(host, args.port), file=sys.stderr)
        return 1

    try:
        with CendojClient() as client:
            result = client.fetch_full_text(
                args.url,
                host=host,
                port=args.port,
                timeout=args.timeout,
            )
    except Exception as exc:  # pragma: no cover - CLI error path
        if isinstance(exc, RuntimeError) and _is_bind_conflict(exc):
            # The port was taken between the probe and the real bind.
            print(_port_conflict_message(host, args.port), file=sys.stderr)
            return 1
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    ref = parse_document_url(args.url)
    pdf_save_result = save_full_text_pdf(result, ref)

    _report(result, args.out, pdf_save_result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
