"""Deterministic MCP server tests.

The server is exercised entirely offline: every HTTP request is intercepted
with ``httpx.MockTransport`` and the captcha answer is monkey-patched. The
suite must never hit the live CENDOJ site.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from navaja import CendojClient
from navaja.captcha import CaptchaAnswer
from navaja.cendoj import INDEX_URL, SEARCH_URL
from navaja.server import (
    buscar_sentencias,
    close_shared_client,
    estado_servidor,
    server,
    ver_texto_completo,
)

FIXTURE = Path(__file__).parent / "fixtures" / "search_clausulas_abusivas.html"
HTML = FIXTURE.read_text(encoding="utf-8")

DOC_URL = (
    "https://www.poderjudicial.es/search/AN/openDocument/"
    "ab58557b5ca08f54a0a8778d75e36f0d/20260916"
)


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
    for name in ("NAVAJA_CAPTCHA_HOST", "NAVAJA_CAPTCHA_PORT", "NAVAJA_CAPTCHA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # Keep token persistence out of the real home directory.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


def test_tools_are_registered_with_expected_names():
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert "buscar_sentencias" in names
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


@pytest.mark.parametrize("texto", ["", "   "])
def test_buscar_sentencias_rejects_empty_query(texto):
    with pytest.raises(ValueError, match="non-empty"):
        buscar_sentencias(texto)


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


def test_estado_servidor_does_not_leak_token(monkeypatch):
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "100.64.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", "1234")
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "super-secret-token-value")

    result = estado_servidor()

    assert result["host"] == "100.64.0.1"
    assert result["port"] == "1234"
    assert result["stable_token_set"] is True
    assert "super-secret-token-value" not in str(result)


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
