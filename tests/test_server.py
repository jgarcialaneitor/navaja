"""Deterministic MCP server tests.

The server is exercised entirely offline: every HTTP request is intercepted
with ``httpx.MockTransport`` and the captcha answer is monkey-patched. The
suite must never hit the live CENDOJ site.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from navaja import CendojClient
from navaja.captcha import (
    CaptchaAnswer,
    CaptchaTimeoutError,
    stop_shared_captcha_server,
)
from navaja import server as navaja_server
from navaja.cendoj import INDEX_URL, SEARCH_URL
from navaja.server import (
    _captcha_lifespan,
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
    for name in ("NAVAJA_CAPTCHA_HOST", "NAVAJA_CAPTCHA_PORT", "NAVAJA_CAPTCHA_TOKEN"):
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
    assert result["captcha_listening"] is False
    assert result["captcha_url_masked"] == "http://100.64.0.1:1234/<token>/"
    assert "super-secret-token-value" not in str(result)
    assert "<token>" in result["captcha_url_masked"]


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


def test_ver_texto_completo_timeout_without_url_omits_key(monkeypatch):
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

    assert result["ok"] is False
    assert result["error_code"] == "captcha_timeout"
    assert "captcha_url" not in result


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


# --- Captcha listener lifespan tests ----------------------------------------


def test_lifespan_starts_and_stops_listener(monkeypatch, capsys):
    port = _free_port()
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")

    async def run() -> None:
        async with _captcha_lifespan(server):
            assert not _port_is_free("127.0.0.1", port)
            result = estado_servidor()
            assert result["captcha_listening"] is True
            assert result["captcha_url_masked"] == f"http://127.0.0.1:{port}/<token>/"

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


def test_lifespan_url_masked_contains_no_token_part(monkeypatch, capsys):
    port = _free_port()
    token = "my-session-token-12345"
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "127.0.0.1")
    monkeypatch.setenv("NAVAJA_CAPTCHA_PORT", str(port))
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", token)

    async def run() -> None:
        async with _captcha_lifespan(server):
            result = estado_servidor()
            assert result["captcha_listening"] is True
            masked = result["captcha_url_masked"]
            assert token not in masked
            assert "<token>" in masked
            assert masked == f"http://127.0.0.1:{port}/<token>/"

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
