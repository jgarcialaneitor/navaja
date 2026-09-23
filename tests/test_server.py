"""Deterministic MCP server tests.

The server is exercised entirely offline: every HTTP request is intercepted
with ``httpx.MockTransport`` and the captcha answer is monkey-patched. The
suite must never hit the live CENDOJ site.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from urllib.parse import parse_qs

from navaja import (
    CendojClient,
    FullTextError,
    FullTextResult,
    NivelLocalizacion,
    SearchError,
    SearchGatedError,
    SearchRequestError,
)
from navaja.captcha import (
    CaptchaAnswer,
    CaptchaServer,
    CaptchaTimeoutError,
    start_shared_captcha_server,
    stop_shared_captcha_server,
)
from navaja import server as navaja_server
import navaja.captcha
from navaja.cendoj import CONTENIDOS_URL, INDEX_URL, LOCALIZACIONES_URL, SEARCH_URL
from navaja.documents import PdfSaveResult
from navaja.server import (
    _captcha_lifespan,
    buscar_sentencias,
    close_shared_client,
    estado_servidor,
    listar_localizaciones,
    server,
    ver_texto_completo,
)

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "search_clausulas_abusivas.html"
HTML = FIXTURE.read_text(encoding="utf-8")

DOC_URL = (
    "https://www.poderjudicial.es/search/AN/openDocument/"
    "ab58557b5ca08f54a0a8778d75e36f0d/20260916"
)

# Minimal valid PDF with extractable text, for offline full-text tests.
PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 12 Tf
100 700 Td
(Hello PDF) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000261 00000 n 
0000000355 00000 n 
trailer
<< /Size 6 /Root 1 0 R >>
startxref
434
%%EOF
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(host: str, port: int) -> bool:
    """Return whether ``host:port`` can be bound without SO_REUSEADDR."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _search_transport(sent: list[httpx.Request] | None = None) -> httpx.MockTransport:
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, text=HTML)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _fulltext_transport(sent: list[httpx.Request] | None = None) -> httpx.MockTransport:
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url.endswith("/search/stickyImg"):
            return httpx.Response(
                200, content=b"fake-png", headers={"content-type": "image/png"}
            )
        if request.method == "POST" and url.startswith(
            "https://www.poderjudicial.es/search/contenidos.action"
        ):
            return httpx.Response(
                200,
                text="<html><body>full text body</body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _combined_transport(sent: list[httpx.Request] | None = None) -> httpx.MockTransport:
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if url == SEARCH_URL:
            return httpx.Response(200, text=HTML)
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url.endswith("/search/stickyImg"):
            return httpx.Response(
                200, content=b"fake-png", headers={"content-type": "image/png"}
            )
        if request.method == "POST" and url.startswith(
            "https://www.poderjudicial.es/search/contenidos.action"
        ):
            return httpx.Response(
                200,
                text="<html><body>full text body</body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _pdf_fulltext_transport(
    sent: list[httpx.Request] | None = None,
    pdf_bytes: bytes = PDF_BYTES,
    content_type: str = 'application/pdf; name="SAP_ML_110_2026.pdf"',
) -> httpx.MockTransport:
    """Return a PDF on the final POST, so ``pdf_bytes`` is populated."""
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url.endswith("/search/stickyImg"):
            return httpx.Response(
                200, content=b"fake-png", headers={"content-type": "image/png"}
            )
        if request.method == "POST" and url.startswith(CONTENIDOS_URL):
            return httpx.Response(
                200,
                content=pdf_bytes,
                headers={"content-type": content_type},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _localizaciones_transport(
    result: str = "&TODAS|MELILLA&MELILLA",
    sent: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    if sent is None:
        sent = []

    body = (
        '{"success":true,"errorCode":-1,"errorMessage":"","result":"'
        + result.replace("&", "\\u0026")
        + '"}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if url == LOCALIZACIONES_URL:
            return httpx.Response(200, text=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _captcha_always_transport(sent: list[httpx.Request] | None = None) -> httpx.MockTransport:
    """Always return the captcha challenge page, so every attempt fails."""
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url.endswith("/search/stickyImg"):
            return httpx.Response(
                200, content=b"fake-png", headers={"content-type": "image/png"}
            )
        if request.method == "POST" and url.startswith(
            "https://www.poderjudicial.es/search/contenidos.action"
        ):
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _spy_client_class(transport_factory, sent: list[httpx.Request]):
    class SpyClient(CendojClient):
        init_count = 0

        def __init__(self, *args, **kwargs):
            SpyClient.init_count += 1
            super().__init__(transport=transport_factory(sent))

    return SpyClient


@pytest.fixture(autouse=True)
def _reset_shared_client():
    """Close and forget the module-level client around every test."""
    close_shared_client()
    yield
    close_shared_client()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Clear server-specific environment variables so tests are deterministic."""
    for name in (
        "NAVAJA_CAPTCHA_HOST",
        "NAVAJA_CAPTCHA_PORT",
        "NAVAJA_CAPTCHA_TOKEN",
        "NAVAJA_MAX_CONCURRENTES",
        "NAVAJA_PDF_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep token persistence out of the real home directory.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


@pytest.fixture(autouse=True)
def _reset_shared_captcha_server(monkeypatch):
    """Stop and clear the shared captcha listener registry around every test."""
    monkeypatch.setattr(navaja_server, "_captcha_lifespan_depth", 0)
    stop_shared_captcha_server()
    yield
    stop_shared_captcha_server()


def test_tools_are_registered_with_expected_names():
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "buscar_sentencias" in names
    assert "listar_localizaciones" in names
    assert "ver_texto_completo" in names
    assert "estado_servidor" in names


def test_buscar_sentencias_returns_documented_shape(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = buscar_sentencias("clausulas abusivas")

    assert result["page"] == 1
    assert result["records_per_page"] == 10
    assert "total" in result
    assert "has_more" in result
    assert isinstance(result["results"], list)
    assert result["results"][0]["roj"]
    assert result["results"][0]["url_documento"]


def test_buscar_sentencias_description_carries_recipe():
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == "buscar_sentencias")
    # Collapse whitespace so line breaks inside phrases do not break assertions.
    description = " ".join(tool.description.split())
    # Load-bearing guidance: page sizes are enumerated, not any stray digit.
    assert "10, 20, 30 or 50" in description
    # The recipe tells the caller to combine place and subject.
    assert (
        "pass the place in ``localizacion`` and a subject wording in ``texto``"
        in description
    )
    # ``materia`` is the reliable classifier.
    assert "Classify results by ``materia``" in description
    # ``total_capped`` is tied to the 200-record ceiling.
    assert (
        "``total_capped`` is ``True`` when the reported total sits at the site's 200-record ceiling"
        in description
    )
    # ``voces`` is explicitly not a substitute for the subject label.
    assert (
        "``voces`` is honoured but does not include every resolution whose label says"
        in description
    )
    # There is no server-side subject filter.
    assert "There is no server-side subject filter" in description


def test_buscar_sentencias_response_carries_materia_and_total_capped(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = buscar_sentencias("clausulas abusivas")

    assert "total_capped" in result
    assert "materia" in result["results"][0]


def test_buscar_sentencias_blank_texto_with_filter_reaches_wire(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(texto="", jurisdiccion="PENAL")

    body = _search_body(sent)
    assert body["JURISDICCION"] == ["PENAL"]
    assert "TEXT" not in body


@pytest.mark.parametrize("texto", ["", "   "])
def test_buscar_sentencias_blank_texto_alone_raises(texto):
    with pytest.raises(ValueError, match="criterion"):
        buscar_sentencias(texto)


# --- listar_localizaciones tests ---------------------------------------------


def test_listar_localizaciones_returns_canonical_nivel_and_tokens(monkeypatch):
    sent: list[httpx.Request] = []

    def transport_factory(s: list[httpx.Request]) -> httpx.MockTransport:
        return _localizaciones_transport("&TODAS|ANDALUCÍA&ANDALUCÍA|MELILLA&MELILLA", s)

    SpyClient = _spy_client_class(transport_factory, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = listar_localizaciones()

    assert result["nivel"] == "COMUNIDAD"
    assert result["localizaciones"] == ["ANDALUCÍA(C)", "MELILLA(C)"]


def test_listar_localizaciones_forwards_level_and_parents(monkeypatch):
    sent: list[httpx.Request] = []

    def transport_factory(s: list[httpx.Request]) -> httpx.MockTransport:
        return _localizaciones_transport("MELILLA&MELILLA", s)

    SpyClient = _spy_client_class(transport_factory, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = listar_localizaciones(
        nivel="SEDE", comunidad="MELILLA", provincia="MELILLA"
    )

    assert result["nivel"] == "SEDE"
    assert result["localizaciones"] == ["MELILLA(S)"]
    body = _localizaciones_body(sent)
    assert body["field"] == ["SEDE"]
    assert body["comunidad"] == ["MELILLA"]
    assert body["provincia"] == ["MELILLA"]


def test_listar_localizaciones_description_carries_vocabulary_and_parents():
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == "listar_localizaciones")
    description = " ".join(tool.description.split())
    assert "site's own location vocabulary" in description.lower()
    assert "buscar_sentencias(localizacion=" in description
    assert "``PROVINCIA`` requires ``comunidad``" in description
    assert "``SEDE`` requires both ``comunidad`` and ``provincia``" in description


def test_listar_localizaciones_value_error_propagates(monkeypatch):
    sent: list[httpx.Request] = []

    def transport_factory(s: list[httpx.Request]) -> httpx.MockTransport:
        return _localizaciones_transport("&TODAS|MELILLA&MELILLA", s)

    SpyClient = _spy_client_class(transport_factory, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    with pytest.raises(ValueError, match="comunidad"):
        listar_localizaciones(nivel="PROVINCIA")


def test_buscar_sentencias_description_points_to_listar_localizaciones():
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == "buscar_sentencias")
    description = " ".join(tool.description.split())
    assert "listar_localizaciones" in description


def test_ver_texto_completo_returns_documented_shape(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert result["attempts"] == 1
    assert result["requests"] >= 2
    assert result["content_type"] is not None
    assert "text" in result
    assert "captcha" not in result
    assert "ABCD" not in str(result)


def test_no_stdout_during_tool_invocations(capsys, monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_combined_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    buscar_sentencias("clausulas abusivas")
    ver_texto_completo(DOC_URL, espera_segundos=1)

    captured = capsys.readouterr()
    assert captured.out == ""


def test_rejects_all_interfaces_captcha_host(monkeypatch):
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "0.0.0.0")
    with pytest.raises(ValueError, match=r"0\.0\.0\.0"):
        ver_texto_completo(DOC_URL, espera_segundos=1)


def test_shared_client_is_created_once_across_two_calls(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias("uno")
    buscar_sentencias("dos")

    assert SpyClient.init_count == 1
    index_gets = [
        r for r in sent if r.method == "GET" and str(r.url) == INDEX_URL
    ]
    assert len(index_gets) == 1, "the JSESSIONID bootstrap must happen only once"


def test_estado_servidor_returns_real_captcha_url(monkeypatch):
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "100.64.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", "1234")
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "super-secret-token-value")

    result = estado_servidor()

    assert result["host"] == "100.64.0.1"
    assert result["port"] == "1234"
    assert result["stable_token_set"] is True
    assert result["captcha_listening"] is False
    assert result["captcha_url"] == "http://100.64.0.1:1234/super-secret-token-value/"
    assert "super-secret-token-value" in result["captcha_url"]
    assert "<token>" not in result["captcha_url"]


def test_estado_servidor_stable_token_set_reports_pre_call_state(monkeypatch, tmp_path):
    """stable_token_set reports the configuration state before the call.

    Resolving a missing token creates and persists one, so the flag must be
    computed from the environment and file state before resolution.
    """
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    result1 = estado_servidor()
    assert result1["stable_token_set"] is False
    assert result1["captcha_url"].startswith(f"http://127.0.0.1:{port}/")
    assert (tmp_path / "navaja" / "captcha-token").exists()

    result2 = estado_servidor()
    assert result2["stable_token_set"] is True
    assert result2["captcha_url"] == result1["captcha_url"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_listener(url: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url)
            if resp.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.05)
    raise TimeoutError(f"listener did not respond at {url}")


# --- estado_servidor self-check tests ---------------------------------------


def test_estado_servidor_fast_path_uses_no_network_call(monkeypatch):
    """With a live local listener, estado_servidor must not hit the network."""
    port = _free_port()
    token = "selfcheck-fast-token01"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    called: list[Any] = []

    def exploding_whoami_check(*args, **kwargs):
        called.append((args, kwargs))
        raise AssertionError("fast path must not call whoami_check")

    monkeypatch.setattr(
        "navaja.server.whoami_check", exploding_whoami_check
    )

    url = start_shared_captcha_server("127.0.0.1", port, token)
    try:
        server_obj = navaja.captcha._captcha_servers[("127.0.0.1", port)]
        result = estado_servidor()

        assert result["captcha_listening"] is True
        assert result["pid"] == os.getpid()
        assert result["nonce"] == server_obj.nonce
        assert result["listener_conflict"] is False
        assert result["listener_pid"] is None
        assert result["captcha_url"] == url
        assert not called
    finally:
        stop_shared_captcha_server()


def test_estado_servidor_reports_conflict_with_other_navaja_listener(monkeypatch):
    """Another navaja process on the same token/port is a conflict."""
    port = _free_port()
    token = "selfcheck-conflict-token1"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    other = CaptchaServer(("127.0.0.1", port), token=token)
    try:
        other.start()
        _wait_for_listener(f"http://127.0.0.1:{port}/{token}/")

        result = estado_servidor()
        assert result["captcha_listening"] is False
        assert result["listener_conflict"] is True
        assert result["listener_pid"] == os.getpid()
        assert result["nonce"] is None
        assert result["pid"] == os.getpid()
        assert result["captcha_url"] == f"http://127.0.0.1:{port}/{token}/"
    finally:
        other.stop()


def test_estado_servidor_no_listener_reports_no_conflict(monkeypatch):
    port = _free_port()
    token = "selfcheck-quiet-token01"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    result = estado_servidor()
    assert result["captcha_listening"] is False
    assert result["listener_conflict"] is False
    assert result["listener_pid"] is None
    assert result["nonce"] is None
    assert result["pid"] == os.getpid()


def test_estado_servidor_non_navaja_listener_reports_no_conflict(monkeypatch):
    """A port held by something that does not speak navaja is not a conflict."""

    class _GarbageHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: D102
            return

        def do_GET(self):  # noqa: N802
            body = b"garbage"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    port = _free_port()
    token = "selfcheck-garbage-token1"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    httpd = HTTPServer(("127.0.0.1", port), _GarbageHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        result = estado_servidor()
        assert result["captcha_listening"] is False
        assert result["listener_conflict"] is False
        assert result["listener_pid"] is None
        assert result["nonce"] is None
        assert result["pid"] == os.getpid()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_stable_token_path_reaches_serve_captcha(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    captured: dict[str, Any] = {}

    def fake_serve_captcha(image_png, *, host, port, timeout, token=None):
        captured["token"] = token
        return CaptchaAnswer("ABCD")

    monkeypatch.setattr("navaja.cendoj.serve_captcha", fake_serve_captcha)
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "my-stable-token-123")

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert captured.get("token") == "my-stable-token-123"


def test_invalid_captcha_token_is_rejected(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "bad/token")

    with pytest.raises(ValueError, match="token"):
        ver_texto_completo(DOC_URL, espera_segundos=1)


def test_server_does_not_patch_secrets_for_stable_token():
    """Regression guard: server.py must not monkey-patch secrets.token_urlsafe."""
    server_source = Path(__file__).parent.parent / "src" / "navaja" / "server.py"
    text = server_source.read_text(encoding="utf-8")
    assert "from unittest.mock import patch" not in text
    assert "import secrets" not in text
    assert "patch.object(secrets" not in text
    assert "token_urlsafe" not in text


def test_ver_texto_completo_timeout_returns_structured_failure(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    exc = CaptchaTimeoutError("no captcha answer received within 0.1 seconds")
    exc.url = "http://127.0.0.1:8765/test-token/"
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: (_ for _ in ()).throw(exc)
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is False
    assert result["error_code"] == "captcha_timeout"
    assert "captcha" in result["error"].lower()
    assert result["attempts"] is None
    assert result["requests"] is None
    assert result["content_type"] is None
    assert result["text"] is None
    assert result["captcha_url"] == "http://127.0.0.1:8765/test-token/"


def test_ver_texto_completo_timeout_without_url_falls_back_to_resolved_url(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CaptchaTimeoutError("timed out")
        ),
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)
    expected_url = estado_servidor()["captcha_url"]

    assert result["ok"] is False
    assert result["error_code"] == "captcha_timeout"
    assert result["captcha_url"] == expected_url


def test_ver_texto_completo_busy_returns_structured_failure(monkeypatch):
    port = _free_port()
    token = "busy-test-token-12345"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    url = start_shared_captcha_server("127.0.0.1", port, token)
    server_obj = navaja.captcha._captcha_servers[("127.0.0.1", port)]
    registered = threading.Event()

    def register_challenge():
        try:
            server_obj.challenge(
                b"fake-png",
                timeout=10.0,
                on_registered=lambda: registered.set(),
            )
        except CaptchaTimeoutError:
            pass

    challenge_thread = threading.Thread(target=register_challenge, daemon=True)
    challenge_thread.start()
    assert registered.wait(timeout=2.0), "pending challenge was not registered"

    try:
        result = ver_texto_completo(DOC_URL, espera_segundos=1)
    finally:
        server_obj.finish(None)
        challenge_thread.join(timeout=2.0)

    assert result["ok"] is False
    assert result["error_code"] == "captcha_busy"
    assert "already pending" in result["error"].lower()
    assert result["captcha_url"] == url
    assert result["attempts"] is None
    assert result["requests"] is None
    assert result["content_type"] is None
    assert result["text"] is None
    assert result["pdf_save_reason"] == "not_attempted"


def test_ver_texto_completo_default_wait_is_120(monkeypatch):
    captured: dict[str, Any] = {}

    class TimeoutClient(CendojClient):
        def fetch_full_text(self, url, *, host, port, timeout, token):
            captured["timeout"] = timeout
            exc = CaptchaTimeoutError("timed out")
            exc.url = "http://127.0.0.1:8765/test-token/"
            raise exc

    monkeypatch.setattr("navaja.server.CendojClient", TimeoutClient)

    result = ver_texto_completo(DOC_URL)

    assert captured["timeout"] == 120.0
    assert result["error_code"] == "captcha_timeout"


def test_ver_texto_completo_rejected_after_max_attempts_returns_structured_failure(
    monkeypatch,
):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_captcha_always_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("WRONG")
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is False
    assert result["error_code"] == "captcha_rejected"
    assert "rejected" in result["error"].lower()
    assert result["attempts"] == 3
    assert result["requests"] == 4
    assert result["content_type"] is not None
    assert result["text"] is not None


def test_ver_texto_completo_malformed_url_returns_structured_failure(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = ver_texto_completo("https://www.poderjudicial.es/not-a-document", espera_segundos=1)

    assert result["ok"] is False
    assert result["error_code"] == "invalid_url"
    assert result["attempts"] is None
    assert result["requests"] is None
    assert result["content_type"] is None
    assert result["text"] is None


def test_ver_texto_completo_programming_error_propagates(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    def bad_serve_captcha(*args, **kwargs):
        raise TypeError("unexpected stub failure")

    monkeypatch.setattr("navaja.cendoj.serve_captcha", bad_serve_captcha)

    with pytest.raises(TypeError, match="unexpected stub failure"):
        ver_texto_completo(DOC_URL, espera_segundos=1)


def test_ver_texto_completo_success_path_still_returns_ok_true(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert "error_code" not in result
    assert result["attempts"] == 1
    assert result["requests"] >= 2
    assert result["content_type"] is not None
    assert "text" in result
    assert result["pdf_path"] is None
    assert result["pdf_save_reason"] == "not_pdf"
    assert result["pdf_save_error"] is None


def test_ver_texto_completo_saves_pdf_and_reports_path(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(pdf_dir))
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_pdf_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert result["pdf_path"] is not None
    assert result["pdf_save_reason"] == "server_sent_name"
    assert result["pdf_save_error"] is None

    saved = Path(result["pdf_path"])
    assert saved.exists()
    assert saved.read_bytes() == PDF_BYTES
    assert saved.name == "SAP_ML_110_2026.pdf"
    assert str(saved).startswith(str(pdf_dir))


def test_ver_texto_completo_html_response_skips_pdf_save(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(pdf_dir))
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert result["pdf_path"] is None
    assert result["pdf_save_reason"] == "not_pdf"
    assert result["pdf_save_error"] is None
    assert not pdf_dir.exists() or not any(pdf_dir.iterdir())


def test_ver_texto_completo_save_failure_still_ok_and_text(monkeypatch, tmp_path):
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(pdf_dir))
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_pdf_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    def failing_save(*args, **kwargs):
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="write_failed",
            error="disk full",
        )

    monkeypatch.setattr("navaja.documents.save_pdf", failing_save)

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert result["text"] == "Hello PDF"
    assert result["pdf_path"] is None
    assert result["pdf_save_reason"] == "write_failed"
    assert result["pdf_save_error"] == "disk full"


def test_ver_texto_completo_bad_pdf_dir_degrades_to_reported_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(tmp_path / "pdfs"))
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_pdf_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    def bad_resolver():
        raise ValueError("malformed NAVAJA_PDF_DIR")

    monkeypatch.setattr("navaja.documents.resolve_pdf_destination", bad_resolver)

    result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["ok"] is True
    assert result["text"] == "Hello PDF"
    assert result["pdf_path"] is None
    assert result["pdf_save_reason"] == "resolve_failed"
    assert "malformed NAVAJA_PDF_DIR" in result["pdf_save_error"]


@pytest.mark.parametrize(
    "scenario, expected_code",
    [
        ("invalid_url", "invalid_url"),
        ("captcha_timeout", "captcha_timeout"),
        ("full_text_error", "full_text_error"),
        ("captcha_rejected", "captcha_rejected"),
    ],
)
def test_ver_texto_completo_error_paths_return_uniform_pdf_keys(
    monkeypatch, tmp_path, scenario, expected_code
):
    pdf_dir = tmp_path / "pdfs"
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(pdf_dir))

    if scenario == "invalid_url":
        result = ver_texto_completo(
            "https://www.poderjudicial.es/not-a-document", espera_segundos=1
        )
    elif scenario == "captcha_timeout":
        sent: list[httpx.Request] = []
        SpyClient = _spy_client_class(_fulltext_transport, sent)
        monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
        exc = CaptchaTimeoutError("no captcha answer received")
        exc.url = "http://127.0.0.1:8765/test-token/"
        monkeypatch.setattr(
            "navaja.cendoj.serve_captcha",
            lambda *args, **kwargs: (_ for _ in ()).throw(exc),
        )
        result = ver_texto_completo(DOC_URL, espera_segundos=1)
    elif scenario == "full_text_error":
        sent: list[httpx.Request] = []
        SpyClient = _spy_client_class(_fulltext_transport, sent)
        monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

        def raise_full_text_error(*args, **kwargs):
            raise FullTextError("site returned an unexpected response")

        monkeypatch.setattr(
            "navaja.server.CendojClient.fetch_full_text", raise_full_text_error
        )
        result = ver_texto_completo(DOC_URL, espera_segundos=1)
    else:  # captcha_rejected
        sent: list[httpx.Request] = []
        SpyClient = _spy_client_class(_captcha_always_transport, sent)
        monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
        monkeypatch.setattr(
            "navaja.cendoj.serve_captcha",
            lambda *args, **kwargs: CaptchaAnswer("WRONG"),
        )
        result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert result["error_code"] == expected_code
    assert "pdf_path" in result
    assert result["pdf_path"] is None
    assert "pdf_save_reason" in result
    assert result["pdf_save_reason"] == "not_attempted"
    assert "pdf_save_error" in result
    assert result["pdf_save_error"] is None


def test_ver_texto_completo_error_path_and_non_pdf_success_have_distinct_reasons(
    monkeypatch,
):
    """An error path must not claim the same reason as a non-PDF response."""
    error_result = ver_texto_completo(
        "https://www.poderjudicial.es/not-a-document", espera_segundos=1
    )
    assert error_result["error_code"] == "invalid_url"
    assert error_result["pdf_save_reason"] == "not_attempted"

    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha",
        lambda *args, **kwargs: CaptchaAnswer("ABCD"),
    )
    success_result = ver_texto_completo(DOC_URL, espera_segundos=1)

    assert success_result["ok"] is True
    assert success_result["pdf_save_reason"] == "not_pdf"
    assert error_result["pdf_save_reason"] != success_result["pdf_save_reason"]


# --- Captcha listener lifespan tests ----------------------------------------


def test_lifespan_starts_and_stops_listener(monkeypatch, capsys):
    port = _free_port()
    token = "valid-token-123456"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    async def run() -> None:
        async with _captcha_lifespan(server):
            assert not _port_is_free("127.0.0.1", port)
            result = estado_servidor()
            assert result["captcha_listening"] is True
            assert result["captcha_url"] == f"http://127.0.0.1:{port}/{token}/"

    asyncio.run(run())

    assert _port_is_free("127.0.0.1", port)
    result = estado_servidor()
    assert result["captcha_listening"] is False
    assert capsys.readouterr().out == ""


def test_lifespan_survives_bad_host_and_tool_still_raises(monkeypatch, capsys):
    """The lifespan must swallow the bad-host rejection, not propagate it.

    The body must actually run (so the session would keep serving) and the
    context must exit normally; the tool path keeps raising the proper error.
    """
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "0.0.0.0")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    stderr_capture = io.StringIO()
    body_ran: list[str] = []

    async def run() -> None:
        with contextlib.redirect_stderr(stderr_capture):
            async with _captcha_lifespan(server):
                # Reached only if the lifespan did not propagate the
                # ValueError raised by resolve_captcha_host().
                body_ran.append("session-alive")
        body_ran.append("exited-cleanly")

    asyncio.run(run())

    assert body_ran == ["session-alive", "exited-cleanly"]
    stderr = stderr_capture.getvalue()
    assert "Captcha listener warning" in stderr
    assert "0.0.0.0" in stderr
    assert capsys.readouterr().out == ""

    with pytest.raises(ValueError, match=r"0\.0\.0\.0"):
        ver_texto_completo(DOC_URL, espera_segundos=1)


def test_lifespan_does_not_crash_on_occupied_port(monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    stderr_capture = io.StringIO()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)

        async def run() -> None:
            with contextlib.redirect_stderr(stderr_capture):
                async with _captcha_lifespan(server):
                    result = estado_servidor()
                    assert result["captcha_listening"] is False

        asyncio.run(run())

    stderr = stderr_capture.getvalue()
    assert "Captcha listener warning" in stderr
    assert "address already in use" in stderr
    assert capsys.readouterr().out == ""


def test_lifespan_url_contains_real_token(monkeypatch, capsys):
    port = _free_port()
    token = "my-session-token-12345"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    async def run() -> None:
        async with _captcha_lifespan(server):
            result = estado_servidor()
            assert result["captcha_listening"] is True
            url = result["captcha_url"]
            assert token in url
            assert "<token>" not in url
            assert url == f"http://127.0.0.1:{port}/{token}/"

    asyncio.run(run())
    assert capsys.readouterr().out == ""


def test_entering_lifespan_context_manager_writes_nothing_to_stdout(
    capsys, monkeypatch
):
    """Entering and exiting the context manager must not touch stdout.

    This asserts the in-process behaviour only; the stdio transport itself is
    not exercised here, so it cannot observe transport-level output.
    """
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    async def run() -> None:
        async with _captcha_lifespan(server):
            pass

    asyncio.run(run())
    assert capsys.readouterr().out == ""


def test_lifespan_releases_listener_when_body_raises(monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    async def run() -> None:
        async with _captcha_lifespan(server):
            assert not _port_is_free("127.0.0.1", port)
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(run())

    assert _port_is_free("127.0.0.1", port), (
        "an exception inside the session body must still release the listener"
    )
    assert estado_servidor()["captcha_listening"] is False
    assert capsys.readouterr().out == ""


def test_nested_lifespan_inner_exit_keeps_listener_for_outer_session(
    monkeypatch, capsys
):
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    async def run() -> None:
        async with _captcha_lifespan(server):
            assert not _port_is_free("127.0.0.1", port)
            async with _captcha_lifespan(server):
                assert not _port_is_free("127.0.0.1", port)
            # The inner exit must not tear down the outer session's listener.
            assert not _port_is_free("127.0.0.1", port)
            assert estado_servidor()["captcha_listening"] is True

    asyncio.run(run())

    assert _port_is_free("127.0.0.1", port)
    assert estado_servidor()["captcha_listening"] is False
    assert capsys.readouterr().out == ""


def test_nested_lifespan_announces_startup_only_once(monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    stderr_capture = io.StringIO()

    async def run() -> None:
        with contextlib.redirect_stderr(stderr_capture):
            async with _captcha_lifespan(server):
                async with _captcha_lifespan(server):
                    pass

    asyncio.run(run())

    stderr = stderr_capture.getvalue()
    assert stderr.count("Captcha form listening at") == 1
    assert capsys.readouterr().out == ""


# --- Search-filter wiring tests -------------------------------------------------


def _last_search_request(sent: list[httpx.Request]) -> httpx.Request:
    return next(
        r for r in reversed(sent) if r.method == "POST" and str(r.url) == SEARCH_URL
    )


def _search_body(sent: list[httpx.Request]) -> dict[str, list[str]]:
    request = _last_search_request(sent)
    return parse_qs(request.content.decode(), keep_blank_values=True)


def _localizaciones_body(sent: list[httpx.Request]) -> dict[str, list[str]]:
    request = next(
        r for r in sent if r.method == "POST" and str(r.url) == LOCALIZACIONES_URL
    )
    return parse_qs(request.content.decode(), keep_blank_values=True)


def test_buscar_sentencias_filter_only_reaches_wire(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(jurisdiccion="PENAL", localizacion=["MELILLA(C)"])

    body = _search_body(sent)
    assert body["JURISDICCION"] == ["PENAL"]
    assert body["VALUESCOMUNIDAD"] == ["MELILLA(C) | "]
    assert "TEXT" not in body


def test_buscar_sentencias_all_modelled_filters_reach_wire(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(
        texto="clausulas abusivas",
        fecha_desde="2024-01-01",
        fecha_hasta="31/12/2024",
        jurisdiccion="CIVIL",
        tipo_resolucion="SENTENCIA",
        roj="SAP NA 123/2024",
        ecli="ECLI:ES:APNA:2024:123",
        num_resolucion="123/2024",
        num_recurso="456/2024",
        ponente="García",
        voces="tráfico de drogas",
        localizacion=["NAVARRA(C)"],
        coleccion="TS",
        orden="antiguo",
    )

    body = _search_body(sent)
    assert body["TEXT"] == ["clausulas abusivas"]
    assert body["FECHARESOLUCIONDESDE"] == ["01/01/2024"]
    assert body["FECHARESOLUCIONHASTA"] == ["31/12/2024"]
    assert body["JURISDICCION"] == ["CIVIL"]
    assert body["TIPORESOLUCION"] == ["SENTENCIA"]
    assert body["ROJ"] == ["SAP NA 123/2024"]
    assert body["ECLI"] == ["ECLI:ES:APNA:2024:123"]
    assert body["NUMERORESOLUCION"] == ["123/2024"]
    assert body["NUMERORECURSO"] == ["456/2024"]
    assert body["PONENTE"] == ["GARCÍA"]
    assert body["VOCES"] == ["TRÁFICO DE DROGAS"]
    assert body["VALUESCOMUNIDAD"] == ["NAVARRA(C) | "]
    assert body["databasematch"] == ["TS"]
    assert body["sort"] == ["IN_FECHARESOLUCION:increasing"]


def test_buscar_sentencias_orden_raw_wire_token_reaches_wire(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(
        texto="clausulas abusivas",
        orden="IN_FECHARESOLUCION:increasing",
    )

    body = _search_body(sent)
    assert body["sort"] == ["IN_FECHARESOLUCION:increasing"]


def test_buscar_sentencias_pagination_reaches_wire(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(
        texto="clausulas abusivas",
        pagina=2,
        records_por_pagina=20,
    )

    body = _search_body(sent)
    assert body["start"] == ["21"]
    assert body["recordsPerPage"] == ["20"]


def test_buscar_sentencias_localizacion_bare_name_and_suffix(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(
        jurisdiccion="PENAL",
        localizacion=["Melilla", "Barcelona(P)"],
    )

    body = _search_body(sent)
    assert body["VALUESCOMUNIDAD"] == ["MELILLA(C) | BARCELONA(P) | "]


@pytest.mark.parametrize("fecha", ["2024-01-01", "01/01/2024"])
def test_buscar_sentencias_accepts_both_date_formats(fecha, monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(jurisdiccion="PENAL", fecha_desde=fecha)

    body = _search_body(sent)
    assert body["FECHARESOLUCIONDESDE"] == ["01/01/2024"]


def test_buscar_sentencias_rejects_nonsense_date(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    with pytest.raises(ValueError, match="fecha_desde"):
        buscar_sentencias(jurisdiccion="PENAL", fecha_desde="not-a-date")


def test_buscar_sentencias_forwards_campos_extra(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    buscar_sentencias(
        texto="clausulas abusivas",
        campos_extra={"ID_NORMA": "1"},
    )

    body = _search_body(sent)
    assert body["ID_NORMA"] == ["1"]


def test_buscar_sentencias_rejects_unsupported_page_size(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    with pytest.raises(ValueError, match="records_per_page"):
        buscar_sentencias(texto="clausulas abusivas", records_por_pagina=99)


def test_buscar_sentencias_rejects_no_criterion(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_search_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    with pytest.raises(ValueError, match="criterion"):
        buscar_sentencias()


def _refusing_search_transport(fixture_name: str, sent: list[httpx.Request] | None = None):
    if sent is None:
        sent = []
    html = (FIXTURES / fixture_name).read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if str(request.url) == SEARCH_URL:
            return httpx.Response(200, text=html)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "fixture, exception_type",
    [
        ("search_invalid_request.html", SearchRequestError),
        ("search_mass_download_gate.html", SearchGatedError),
    ],
)
def test_buscar_sentencias_propagates_search_error(
    fixture, exception_type, monkeypatch
):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(
        lambda sent=sent: _refusing_search_transport(fixture, sent), sent
    )
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    with pytest.raises(exception_type):
        buscar_sentencias(texto="clausulas abusivas")


def test_buscar_sentencias_schema_lists_new_parameters():
    tools = asyncio.run(server.list_tools())
    tool = next(t for t in tools if t.name == "buscar_sentencias")
    properties = tool.input_schema.get("properties", {})
    for name in (
        "jurisdiccion",
        "tipo_resolucion",
        "fecha_desde",
        "fecha_hasta",
        "localizacion",
        "campos_extra",
    ):
        assert name in properties, f"{name} missing from schema"


def _marker_fulltext_transport(
    marker: str, sent: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """Like ``_fulltext_transport`` but the final HTML body carries ``marker``."""
    if sent is None:
        sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                text=(
                    '<html><img src="/search/stickyImg">'
                    '<form action="captcha"></form></html>'
                ),
                headers={"content-type": "text/html"},
            )
        if url.endswith("/search/stickyImg"):
            return httpx.Response(
                200, content=b"fake-png", headers={"content-type": "image/png"}
            )
        if request.method == "POST" and url.startswith(
            "https://www.poderjudicial.es/search/contenidos.action"
        ):
            return httpx.Response(
                200,
                text=f"<html><body>{marker}</body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _wait_for_jobs(done_check, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done_check():
            return True
        time.sleep(0.01)
    return False


# --- Batch download tools ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_shared_queue():
    """Stop and forget the module-level job queue around every test."""
    queue = getattr(navaja_server, "_queue", None)
    if queue is not None:
        queue.stop()
    navaja_server._queue = None
    yield
    queue = getattr(navaja_server, "_queue", None)
    if queue is not None:
        queue.stop()
    navaja_server._queue = None


def test_batch_tools_are_registered():
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    for name in ("iniciar_descargas", "estado_descargas", "recoger_descarga"):
        assert name in names


def test_iniciar_descargas_returns_documented_shape(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    urls = [DOC_URL, DOC_URL]
    result = navaja_server.iniciar_descargas(urls)

    assert result["ok"] is True
    assert result["captcha_url"].startswith("http://")
    assert result["max_concurrentes"] == 1
    assert isinstance(result["batch_id"], str)
    assert len(result["jobs"]) == 2
    for job, url in zip(result["jobs"], urls):
        assert set(job.keys()) == {"job_id", "url", "state"}
        assert isinstance(job["job_id"], str)
        assert job["url"] == url
        assert job["state"] in {"queued", "running", "done"}
    assert "ABCD" not in json.dumps(result)


def test_iniciar_descargas_rejects_empty_urls():
    result = navaja_server.iniciar_descargas([])
    assert result["ok"] is False
    assert result["error_code"] == "empty_urls"
    assert "empty" in result["error"].lower()
    assert json.dumps(result)


def test_estado_descargas_leaks_no_payload_text(monkeypatch):
    marker = "UNIQUE_MARKER_42"
    sent: list[httpx.Request] = []

    def transport_factory(s: list[httpx.Request]) -> httpx.MockTransport:
        return _marker_fulltext_transport(marker, s)

    SpyClient = _spy_client_class(transport_factory, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    start = navaja_server.iniciar_descargas([DOC_URL])
    job_id = start["jobs"][0]["job_id"]

    def is_done():
        for job in navaja_server.estado_descargas()["jobs"]:
            if job["job_id"] == job_id and job["state"] == "done":
                return True
        return False

    assert _wait_for_jobs(is_done), "job did not finish"

    result = navaja_server.estado_descargas()
    serialised = json.dumps(result)
    assert marker not in serialised
    for job in result["jobs"]:
        assert set(job.keys()) == {
            "job_id",
            "url",
            "state",
            "attempts",
            "pdf_path",
            "error_code",
        }


def test_recoger_descarga_pending_finished_and_unknown(monkeypatch):
    blocker = threading.Event()

    class BlockingClient(CendojClient):
        def fetch_full_text(self, url, *, host, port, timeout, token):
            assert blocker.wait(timeout=10.0)
            return FullTextResult(
                ok=True,
                content_type="text/html",
                text="collected body",
                pdf_bytes=None,
                attempts=1,
                requests=2,
            )

    monkeypatch.setattr("navaja.server.CendojClient", BlockingClient)

    start = navaja_server.iniciar_descargas([DOC_URL])
    job_id = start["jobs"][0]["job_id"]

    pending = navaja_server.recoger_descarga(job_id)
    assert pending["ok"] is False
    assert pending["error_code"] == "pending"
    assert pending["job_id"] == job_id

    unknown = navaja_server.recoger_descarga("no-such-job-id")
    assert unknown["ok"] is False
    assert unknown["error_code"] == "unknown_job"
    assert unknown["job_id"] == "no-such-job-id"

    blocker.set()

    def is_done():
        for job in navaja_server.estado_descargas()["jobs"]:
            if job["job_id"] == job_id and job["state"] == "done":
                return True
        return False

    assert _wait_for_jobs(is_done), "job did not finish"

    finished = navaja_server.recoger_descarga(job_id)
    assert finished["ok"] is True
    assert finished["text"] == "collected body"
    assert finished["attempts"] == 1
    assert finished["requests"] == 2
    assert finished["pdf_path"] is None
    assert finished["pdf_save_reason"] == "not_pdf"
    assert "error_code" not in finished


def test_batch_multi_url_completes(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    urls = [DOC_URL, DOC_URL, DOC_URL]
    start = navaja_server.iniciar_descargas(urls)

    def all_done():
        jobs = navaja_server.estado_descargas()["jobs"]
        return len(jobs) == len(urls) and all(job["state"] == "done" for job in jobs)

    assert _wait_for_jobs(all_done), "not all jobs finished"

    for job in start["jobs"]:
        payload = navaja_server.recoger_descarga(job["job_id"])
        assert payload["ok"] is True
        assert "text" in payload


def test_recoger_descarga_normalises_runner_crash(monkeypatch, capsys):
    class CrashClient(CendojClient):
        def fetch_full_text(self, url, *, host, port, timeout, token):
            raise ValueError("simulated unexpected runner crash")

    monkeypatch.setattr("navaja.server.CendojClient", CrashClient)

    start = navaja_server.iniciar_descargas([DOC_URL])
    job_id = start["jobs"][0]["job_id"]

    def is_failed():
        for job in navaja_server.estado_descargas()["jobs"]:
            if job["job_id"] == job_id and job["state"] == "failed":
                return True
        return False

    assert _wait_for_jobs(is_failed), "job did not fail"

    result = navaja_server.recoger_descarga(job_id)
    assert result["ok"] is False
    assert result["error_code"] == "runner_failure"
    assert "simulated unexpected runner crash" in result["error"]
    assert result["attempts"] is None
    assert result["requests"] is None
    assert result["content_type"] is None
    assert result["text"] is None
    assert result["pdf_path"] is None
    assert result["pdf_save_reason"] == "not_attempted"
    assert result["pdf_save_error"] is None
    assert json.dumps(result)
    assert capsys.readouterr().out == ""


def test_max_concurrentes_default(monkeypatch):
    monkeypatch.delenv("NAVAJA_MAX_CONCURRENTES", raising=False)
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = navaja_server.iniciar_descargas([DOC_URL])
    assert result["max_concurrentes"] == 1


def test_max_concurrentes_valid_override(monkeypatch):
    monkeypatch.setenv("NAVAJA_MAX_CONCURRENTES", "3")
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = navaja_server.iniciar_descargas([DOC_URL])
    assert result["max_concurrentes"] == 3


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5"])
def test_max_concurrentes_invalid_value(bad, monkeypatch):
    monkeypatch.setenv("NAVAJA_MAX_CONCURRENTES", bad)
    with pytest.raises(ValueError, match="NAVAJA_MAX_CONCURRENTES"):
        navaja_server.iniciar_descargas([DOC_URL])


def test_no_stdout_on_batch_tools(capsys, monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    start = navaja_server.iniciar_descargas([DOC_URL])
    navaja_server.estado_descargas()
    navaja_server.recoger_descarga(start["jobs"][0]["job_id"])
    assert capsys.readouterr().out == ""


# --- batch registry hardening tests ------------------------------------------


def test_iniciar_descargas_rejects_more_than_100_urls(monkeypatch):
    result = navaja_server.iniciar_descargas([DOC_URL] * 101)
    assert result["ok"] is False
    assert result["error_code"] == "too_many_urls"
    assert "100" in result["error"]


def test_iniciar_descargas_rejects_invalid_url_and_enqueues_nothing(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)

    result = navaja_server.iniciar_descargas([DOC_URL, "https://example.com/not-valid"])
    assert result["ok"] is False
    assert result["error_code"] == "invalid_url"
    assert "https://example.com/not-valid" in result["error"]

    # No HTTP traffic and no queued jobs: the call was rejected before
    # touching the client or the queue.
    assert not sent
    queue = navaja_server._get_queue()
    assert queue.estado() == []


def test_iniciar_descargas_rejected_call_does_not_affect_later_batch(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    rejected = navaja_server.iniciar_descargas(["https://example.com/bad"])
    assert rejected["error_code"] == "invalid_url"

    valid = navaja_server.iniciar_descargas([DOC_URL])
    assert valid["ok"] is True
    assert len(valid["jobs"]) == 1

    def is_done():
        jobs = navaja_server.estado_descargas()["jobs"]
        return len(jobs) == 1 and jobs[0]["state"] == "done"

    assert _wait_for_jobs(is_done), "valid batch did not finish"


def test_estado_descargas_filters_by_batch_id(monkeypatch):
    sent: list[httpx.Request] = []
    SpyClient = _spy_client_class(_fulltext_transport, sent)
    monkeypatch.setattr("navaja.server.CendojClient", SpyClient)
    monkeypatch.setattr(
        "navaja.cendoj.serve_captcha", lambda *args, **kwargs: CaptchaAnswer("ABCD")
    )

    alpha = navaja_server.iniciar_descargas([DOC_URL])
    beta = navaja_server.iniciar_descargas([DOC_URL, DOC_URL])

    alpha_jobs = navaja_server.estado_descargas(alpha["batch_id"])["jobs"]
    beta_jobs = navaja_server.estado_descargas(beta["batch_id"])["jobs"]

    assert len(alpha_jobs) == 1
    assert len(beta_jobs) == 2
    assert {j["job_id"] for j in alpha_jobs}.isdisjoint(j["job_id"] for j in beta_jobs)

    # Default is the most recent batch.
    default_jobs = navaja_server.estado_descargas()["jobs"]
    assert len(default_jobs) == 2
    assert {j["job_id"] for j in default_jobs} == {j["job_id"] for j in beta_jobs}


def test_estado_descargas_unknown_batch_id_returns_empty():
    result = navaja_server.estado_descargas("no-such-batch-id")
    assert result["ok"] is True
    assert result["jobs"] == []
