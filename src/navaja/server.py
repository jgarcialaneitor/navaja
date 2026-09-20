"""MCP server for personal CENDOJ case-law research.

The server exposes three tools:

* ``buscar_sentencias`` — free-text search, no captcha.
* ``ver_texto_completo`` — human-in-the-loop full-text fetch.
* ``estado_servidor`` — runtime configuration snapshot.

It runs over the stdio transport, so stdout is reserved for JSON-RPC. Any
human-facing message (including the captcha form URL) is emitted on stderr.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import os
import sys
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer

from navaja import CendojClient, FullTextError
from navaja.captcha import (
    CaptchaTimeoutError,
    default_captcha_token_path,
    resolve_captcha_host,
    resolve_captcha_token,
)
from navaja.documents import parse_document_url

server = MCPServer("navaja", version="0.0.1")

_client_lock = threading.Lock()
_client: CendojClient | None = None


def _get_client() -> CendojClient:
    """Return the module-level client, creating it lazily under a lock."""
    global _client
    with _client_lock:
        if _client is None:
            _client = CendojClient()
        return _client


def close_shared_client() -> None:
    """Close the module-level client and release its connection pool."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_shared_client)


def _captcha_port() -> int:
    raw = os.environ.get("NAVAJA_CAPTCHA_PORT", "8765").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"NAVAJA_CAPTCHA_PORT must be an integer, got {raw!r}"
        ) from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"NAVAJA_CAPTCHA_PORT must be 1-65535, got {port}")
    return port


@server.tool()
def buscar_sentencias(
    texto: str,
    pagina: int = 1,
    records_por_pagina: int = 10,
) -> dict:
    """Search the CENDOJ case-law database by free text.

    Returns structured metadata for each resolution: ROJ, ECLI, date, organ,
    seat, resolution and appeal numbers, municipality, ponente, the site's
    own automatic summary, and the document URL.

    This tool does not solve any captcha; the search endpoint does not require
    one.

    Args:
        texto: free-text query, e.g. ``"clausulas abusivas"``.
        pagina: 1-based page number.
        records_por_pagina: hits per page.
    """
    if not texto or not texto.strip():
        raise ValueError("texto must be a non-empty search string")

    client = _get_client()
    page = client.search(
        texto,
        page=pagina,
        records_per_page=records_por_pagina,
    )
    return page.as_dict()


@server.tool()
def ver_texto_completo(
    url: str,
    espera_segundos: int = 300,
) -> dict:
    """Fetch the full text of a single CENDOJ resolution.

    This is a human-in-the-loop operation. If the site requests a captcha, the
    server opens a local browser form and blocks until a human types the answer.
    The exact URL to open is announced on stderr.

    Navaja deliberately contains no automatic captcha solver, OCR, vision model
    or third-party solving service.

    Args:
        url: a CENDOJ ``openDocument`` URL.
        espera_segundos: how long to wait for the human answer.

    Returns:
        A dict with ``ok``, ``attempts``, ``requests``, ``content_type`` and
        ``text``. On failure ``ok`` is ``False`` and the dict also carries
        ``error`` and ``error_code`` (``captcha_timeout``,
        ``captcha_rejected``, ``full_text_error`` or ``invalid_url``). When
        a captcha timeout occurs, ``captcha_url`` contains the form URL so
        the caller can retry at the same address. The captcha answer itself
        is never returned.
    """
    try:
        parse_document_url(url)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "invalid_url",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
        }

    host, host_reason = resolve_captcha_host()
    port = _captcha_port()
    token, _token_reason = resolve_captcha_token()

    print(
        f"Captcha form will bind to {host} because {host_reason}",
        file=sys.stderr,
        flush=True,
    )

    client = _get_client()

    try:
        # The stdio transport owns stdout. Redirect any stray print from the
        # underlying client (prompts and the captcha form URL) to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            result = client.fetch_full_text(
                url,
                host=host,
                port=port,
                timeout=float(espera_segundos),
                token=token,
            )
    except CaptchaTimeoutError as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "error_code": "captcha_timeout",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
        }
        if hasattr(exc, "url"):
            payload["captcha_url"] = exc.url
        return payload
    except FullTextError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "full_text_error",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
        }

    payload = {
        "ok": result.ok,
        "attempts": result.attempts,
        "requests": result.requests,
        "content_type": result.content_type,
        "text": result.text,
    }
    if result.error is not None:
        payload["error"] = result.error
        payload["error_code"] = "captcha_rejected"
    return payload


@server.tool()
def estado_servidor() -> dict:
    """Return runtime server configuration without leaking secrets.

    Returns the configured captcha host and port, and whether a stable token
    has been set. The token value itself is never exposed.
    """
    host, _host_reason = resolve_captcha_host()
    port = _captcha_port()

    env_token_set = bool(os.environ.get("NAVAJA_CAPTCHA_TOKEN", "").strip())
    stable_token_set = env_token_set
    if not stable_token_set:
        # A persisted token also gives a stable URL even without the env var.
        stable_token_set = default_captcha_token_path().exists()

    return {
        "host": host,
        "port": str(port),
        "stable_token_set": stable_token_set,
    }


def main(argv: list[str] | None = None) -> None:
    """Run the navaja MCP server over stdio."""
    parser = argparse.ArgumentParser(
        prog="navaja-mcp",
        description="MCP server for personal CENDOJ case-law research",
    )
    parser.parse_args(argv)
    server.run(transport="stdio")
