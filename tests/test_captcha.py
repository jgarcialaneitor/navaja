"""Offline tests for the local captcha answer server.

These tests never hit the live CENDOJ site. They start the local form server
on loopback, interact with it over HTTP, and verify the security boundaries.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any

import httpx
import pytest

from navaja import captcha
from navaja.captcha import (
    CaptchaBusyError,
    CaptchaServer,
    CaptchaTimeoutError,
    _get_or_create_captcha_server,
    resolve_captcha_host,
    resolve_captcha_token,
    serve_captcha,
    stop_shared_captcha_server,
)

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


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


def _wait_for_url(stderr: io.StringIO, deadline: float) -> str | None:
    while time.monotonic() < deadline:
        text = stderr.getvalue()
        if "http://" in text:
            return text.strip().split()[-1]
        time.sleep(0.05)
    return None


def _run_server(
    image: bytes,
    port: int,
    timeout: float = 5.0,
    token: str | None = None,
) -> tuple[dict[str, Any], threading.Thread, io.StringIO]:
    result: dict[str, Any] = {}
    stderr_capture = io.StringIO()

    def target() -> None:
        with contextlib.redirect_stderr(stderr_capture):
            try:
                result["answer"] = serve_captcha(
                    image,
                    host="127.0.0.1",
                    port=port,
                    timeout=timeout,
                    token=token,
                )
            except Exception as exc:  # pragma: no cover
                result["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return result, thread, stderr_capture


def _token_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _challenge_id_from_page(page: httpx.Response) -> str:
    match = re.search(
        r'<input[^>]*name="challenge"[^>]*value="([^"]+)"',
        page.text,
    )
    assert match is not None, "form page is missing challenge hidden field"
    return match.group(1)


@pytest.fixture(autouse=True)
def _reset_shared_captcha_server():
    """Stop and clear the shared captcha listener registry around every test."""
    stop_shared_captcha_server()
    yield
    stop_shared_captcha_server()


def test_refuses_all_ipv4_interfaces():
    with pytest.raises(ValueError, match="0\\.0\\.0\\.0"):
        serve_captcha(TINY_PNG, host="0.0.0.0", port=0, timeout=0.1)


def test_refuses_ipv6_all_interfaces():
    with pytest.raises(ValueError, match="::"):
        serve_captcha(TINY_PNG, host="::", port=0, timeout=0.1)


def test_refuses_empty_host():
    with pytest.raises(ValueError):
        serve_captcha(TINY_PNG, host="", port=0, timeout=0.1)


def test_times_out_when_no_answer():
    port = _free_port()
    with pytest.raises(CaptchaTimeoutError):
        serve_captcha(TINY_PNG, host="127.0.0.1", port=port, timeout=0.2)


def test_timeout_error_carries_form_url():
    port = _free_port()
    token = "valid-token-12345"
    with pytest.raises(CaptchaTimeoutError) as exc_info:
        serve_captcha(TINY_PNG, host="127.0.0.1", port=port, timeout=0.2, token=token)

    assert hasattr(exc_info.value, "url")
    assert f"http://127.0.0.1:{port}/{token}/" == exc_info.value.url


def test_serves_image_at_token_path_and_returns_answer():
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    token = _token_from_url(url)

    page = httpx.get(url)
    assert page.status_code == 200
    assert "<form" in page.text
    challenge_id = _challenge_id_from_page(page)

    image = httpx.get(f"http://127.0.0.1:{port}/{token}/captcha.png")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content == TINY_PNG

    wrong_token = httpx.get(f"http://127.0.0.1:{port}/wrong-token/captcha.png")
    assert wrong_token.status_code == 404

    absent_token = httpx.get(f"http://127.0.0.1:{port}/captcha.png")
    assert absent_token.status_code == 404

    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "abc 123", "challenge": challenge_id},
    )
    thread.join(timeout=5.0)

    assert result.get("answer") == "abc 123"


def test_wrong_token_post_returns_404():
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None
    token = _token_from_url(url)

    resp = httpx.post(
        f"http://127.0.0.1:{port}/wrong-token/",
        data={"captcha": "x", "challenge": "1"},
    )
    assert resp.status_code == 404

    # Shut down the real server cleanly so the test thread does not hang.
    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "x", "challenge": challenge_id},
    )
    thread.join(timeout=5.0)


def test_supplied_valid_token_is_honored():
    port = _free_port()
    token = "valid-token-12345"
    result, thread, stderr = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    assert f"/{token}/" in url

    page = httpx.get(f"http://127.0.0.1:{port}/{token}/")
    assert page.status_code == 200
    assert "<form" in page.text
    challenge_id = _challenge_id_from_page(page)

    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "solved", "challenge": challenge_id},
    )
    thread.join(timeout=5.0)

    assert result.get("answer") == "solved"


@pytest.mark.parametrize(
    "bad_token",
    [
        "",
        "   ",
        "short",
        "has/slash",
        "has space",
        "has%percent",
    ],
)
def test_invalid_token_raises_value_error(bad_token):
    with pytest.raises(ValueError, match="token"):
        serve_captcha(TINY_PNG, token=bad_token, timeout=0.1)


# --- Socket lifecycle regression tests --------------------------------------


def test_timeout_keeps_listener_bound_on_repeated_timeouts():
    """A timeout no longer tears the listener down.

    The port stays bound across repeated timeouts on the same fixed port and
    URL. stop_shared_captcha_server() is what finally releases the port.
    """
    port = _free_port()
    token = "reuse-token-12345"
    first_url: str | None = None
    for i in range(3):
        with pytest.raises(CaptchaTimeoutError):
            serve_captcha(
                TINY_PNG, host="127.0.0.1", port=port, timeout=0.1, token=token
            )
        assert not _port_is_free("127.0.0.1", port), (
            f"listener released port {port} after timeout iteration {i}"
        )
        if first_url is None:
            first_url = f"http://127.0.0.1:{port}/{token}/"
    assert first_url is not None

    stop_shared_captcha_server()
    assert _port_is_free("127.0.0.1", port), (
        f"port {port} still bound after stop_shared_captcha_server()"
    )


def test_successful_answer_keeps_listener_bound_and_allows_second_challenge():
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port, timeout=5.0)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(url, data={"captcha": "solved", "challenge": challenge_id})
    thread.join(timeout=5.0)

    assert result.get("answer") == "solved"
    # The listener must stay bound after the challenge is answered.
    assert not _port_is_free("127.0.0.1", port)

    # A second challenge on the same listener should work without a rebind.
    result2: dict[str, Any] = {}

    def target2() -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                result2["answer"] = serve_captcha(
                    TINY_PNG, host="127.0.0.1", port=port, timeout=5.0
                )
            except Exception as exc:  # pragma: no cover
                result2["exc"] = exc

    thread2 = threading.Thread(target=target2, daemon=True)
    thread2.start()
    # Wait for the second challenge to be registered.
    page2 = httpx.get(url)
    challenge_id2 = _challenge_id_from_page(page2)
    httpx.post(url, data={"captcha": "again", "challenge": challenge_id2})
    thread2.join(timeout=5.0)

    assert result2.get("answer") == "again"


def test_handler_exception_makes_caller_timeout_and_keeps_listener_bound(
    monkeypatch,
):
    def raising_do_GET(self):  # noqa: N802
        raise RuntimeError("simulated handler failure")

    monkeypatch.setattr("navaja.captcha._Handler.do_GET", raising_do_GET)
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port, timeout=0.5)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    # Trigger the broken handler; the blocked caller must time out, not hang.
    # An unhandled handler exception drops the connection without a response.
    with pytest.raises(httpx.RemoteProtocolError):
        httpx.get(url)
    thread.join(timeout=5.0)

    assert isinstance(result.get("exc"), CaptchaTimeoutError)
    assert not _port_is_free("127.0.0.1", port)


def test_bind_to_occupied_port_raises_clear_error():
    port = _free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        with pytest.raises(
            RuntimeError,
            match=(
                rf"^captcha server cannot bind to '127\.0\.0\.1' port {port}: "
                "address already in use; another navaja instance is likely "
                "listening — kill it or set NAVAJA_CAPTCHA_PORT$"
            ),
        ):
            serve_captcha(TINY_PNG, host="127.0.0.1", port=port, timeout=0.1)


@pytest.mark.parametrize(
    "winerror",
    [10013, 10048],
    ids=["WSAEACCES", "WSAEADDRINUSE"],
)
def test_bind_refusal_on_windows_raises_clear_error(monkeypatch, winerror):
    """A Windows-style bind refusal is reported as address already in use.

    The raw-socket Windows CI test raised WSAEACCES (10013) for an occupied
    address, while two real navaja processes on a real Windows machine
    succeeded in binding to the same port because HTTPServer's SO_REUSEADDR
    permits active-listener hijack on Windows. CaptchaServer now disables
    reuse and sets SO_EXCLUSIVEADDRUSE on Windows, so either code can be
    reported when another process holds the port.
    """

    def raising_server(*args, **kwargs):
        exc = OSError("simulated bind refusal")
        exc.winerror = winerror
        raise exc

    monkeypatch.setattr("navaja.captcha.CaptchaServer", raising_server)
    with pytest.raises(
        RuntimeError,
        match=(
            r"^captcha server cannot bind to '127\.0\.0\.1' port 12345: "
            r"address already in use; another navaja instance is likely listening — "
            r"kill it or set NAVAJA_CAPTCHA_PORT$"
        ),
    ):
        serve_captcha(TINY_PNG, host="127.0.0.1", port=12345, timeout=0.1)


def test_second_captcha_server_on_same_address_raises_clear_error():
    """A second listener on the same fixed port must fail loudly.

    On POSIX this already fails because SO_REUSEADDR only reuses TIME_WAIT
    sockets. On Windows it failed before the fix: both sockets carried
    SO_REUSEADDR, so the second bind succeeded and connections were silently
    routed to either process.
    """
    port = _free_port()
    token = "conflict-token-0001"
    first = CaptchaServer(("127.0.0.1", port), token=token)
    try:
        with pytest.raises(
            RuntimeError,
            match=(
                rf"^captcha server cannot bind to '127\.0\.0\.1' port {port}: "
                "address already in use; another navaja instance is likely "
                "listening — kill it or set NAVAJA_CAPTCHA_PORT$"
            ),
        ):
            _get_or_create_captcha_server("127.0.0.1", port, token)
    finally:
        first.stop()


def test_captcha_server_disables_reuse_address_when_simulating_windows(monkeypatch):
    """On Windows the instance must opt out of HTTPServer's SO_REUSEADDR default."""
    monkeypatch.setattr("navaja.captcha.os.name", "nt")
    port = _free_port()
    server = CaptchaServer(("127.0.0.1", port), token="windows-reuse-token1")
    try:
        assert server.allow_reuse_address is False
    finally:
        server.stop()


def test_captcha_server_keeps_reuse_address_on_posix():
    """On POSIX the inherited SO_REUSEADDR default must remain enabled."""
    if os.name == "nt":
        return
    port = _free_port()
    server = CaptchaServer(("127.0.0.1", port), token="posix-reuse-token-01")
    try:
        assert server.allow_reuse_address == 1
    finally:
        server.stop()


def test_captcha_server_sets_exclusive_addr_use_when_simulating_windows(monkeypatch):
    """On Windows, server_bind sets SO_EXCLUSIVEADDRUSE before binding."""
    monkeypatch.setattr("navaja.captcha.os.name", "nt")
    monkeypatch.setattr(
        "socket.SO_EXCLUSIVEADDRUSE", 9999, raising=False
    )

    port = _free_port()
    calls: list[tuple[Any, ...]] = []
    original_setsockopt = socket.socket.setsockopt
    original_bind = socket.socket.bind

    def spy_setsockopt(self, level, optname, value):
        calls.append(("setsockopt", level, optname, value))
        # The fake Windows-only option does not exist on Linux; swallow it.
        if optname == 9999:
            return 0
        return original_setsockopt(self, level, optname, value)

    def spy_bind(self, address):
        calls.append(("bind", address))
        return original_bind(self, address)

    monkeypatch.setattr("socket.socket.setsockopt", spy_setsockopt)
    monkeypatch.setattr("socket.socket.bind", spy_bind)

    server = CaptchaServer(("127.0.0.1", port), token="exclusive-token-001")
    try:
        exclusive_index = calls.index(
            ("setsockopt", socket.SOL_SOCKET, 9999, 1)
        )
        bind_index = calls.index(("bind", ("127.0.0.1", port)))
        # Membership alone would also pass if the flag were set after the
        # bind, where Windows would ignore it; the guarantee is the order.
        assert exclusive_index < bind_index
    finally:
        server.stop()


def test_captcha_server_skips_exclusive_addr_use_on_posix(monkeypatch):
    """On POSIX, server_bind must not set the Windows-only exclusive option."""
    if os.name == "nt":
        return
    monkeypatch.setattr(
        "socket.SO_EXCLUSIVEADDRUSE", 9999, raising=False
    )

    calls: list[tuple[Any, ...]] = []
    original_setsockopt = socket.socket.setsockopt

    def spy_setsockopt(self, level, optname, value):
        calls.append((level, optname, value))
        return original_setsockopt(self, level, optname, value)

    monkeypatch.setattr("socket.socket.setsockopt", spy_setsockopt)

    port = _free_port()
    server = CaptchaServer(("127.0.0.1", port), token="posix-exclusive-token1")
    try:
        assert (socket.SOL_SOCKET, 9999, 1) not in calls
        assert server.allow_reuse_address == 1
    finally:
        server.stop()


def _start_idle_listener(port: int, token: str) -> str:
    """Start the shared listener and let the only challenge time out.

    Returns the form URL; the listener remains bound but idle.
    """
    result: dict[str, Any] = {}
    stderr_capture = io.StringIO()

    def target() -> None:
        with contextlib.redirect_stderr(stderr_capture):
            try:
                serve_captcha(
                    TINY_PNG,
                    host="127.0.0.1",
                    port=port,
                    timeout=0.1,
                    token=token,
                )
            except CaptchaTimeoutError:
                result["timeout"] = True
            except Exception as exc:  # pragma: no cover
                result["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    url = _wait_for_url(stderr_capture, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    thread.join(timeout=2.0)
    assert result.get("timeout") is True
    return url


def test_idle_page_contains_refresh_directive_and_running_message():
    port = _free_port()
    token = "idle-page-token-01"
    url = _start_idle_listener(port, token)

    page = httpx.get(url)
    assert page.status_code == 200
    assert "http-equiv=\"refresh\"" in page.text
    assert "content=\"5\"" in page.text
    assert "No hay ningún captcha pendiente" in page.text
    assert "Navaja está en ejecución" in page.text


def test_ack_page_keeps_title_and_refreshes_after_answer():
    """The post-answer page auto-refreshes so a batch queue does not stall.

    The title must stay ``CENDOJ captcha`` because ``src/navaja/cli.py`` uses
    it as a navaja-listener marker.
    """
    port = _free_port()
    token = "ack-page-token-001"
    result, thread, stderr = _run_server(TINY_PNG, port, timeout=5.0, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)

    resp = httpx.post(url, data={"captcha": "solved", "challenge": challenge_id})
    assert resp.status_code == 200
    assert "<title>CENDOJ captcha</title>" in resp.text
    assert "http-equiv=\"refresh\"" in resp.text
    assert "content=\"5\"" in resp.text
    assert "Respuesta recibida" in resp.text
    assert "se actualizar&aacute; autom&aacute;ticamente" in resp.text
    assert "Pod&eacute;s cerrar esta pesta&ntilde;a" not in resp.text

    thread.join(timeout=5.0)
    assert result.get("answer") == "solved"


def test_png_returns_404_while_idle():
    port = _free_port()
    token = "idle-png-token-002"
    _start_idle_listener(port, token)

    resp = httpx.get(f"http://127.0.0.1:{port}/{token}/captcha.png")
    assert resp.status_code == 404
    assert "Not found" in resp.text


def test_post_while_idle_returns_idle_page_and_changes_no_state():
    port = _free_port()
    token = "idle-post-token-03"
    url = _start_idle_listener(port, token)

    resp = httpx.post(url, data={"captcha": "ignored"})
    assert resp.status_code == 200
    assert "no había ningún captcha pendiente" in resp.text.lower()

    # A subsequent GET must still be idle, not suddenly create a challenge.
    page = httpx.get(url)
    assert page.status_code == 200
    assert "No hay ningún captcha pendiente" in page.text


def test_concurrent_challenge_raises_captcha_busy_error():
    port = _free_port()
    token = "busy-token-0000001"
    result, thread, stderr = _run_server(TINY_PNG, port, timeout=5.0, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    with pytest.raises(CaptchaBusyError):
        serve_captcha(
            TINY_PNG, host="127.0.0.1", port=port, token=token, timeout=0.1
        )

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(url, data={"captcha": "done", "challenge": challenge_id})
    thread.join(timeout=5.0)
    assert result.get("answer") == "done"


def test_stop_shared_captcha_server_releases_port():
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port, timeout=2.0)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    assert not _port_is_free("127.0.0.1", port)

    stop_shared_captcha_server()
    assert _port_is_free("127.0.0.1", port)
    thread.join(timeout=5.0)
    assert isinstance(result.get("exc"), CaptchaTimeoutError)


def test_wrong_token_returns_404_while_idle():
    port = _free_port()
    token = "idle-wrong-token-04"
    _start_idle_listener(port, token)

    resp = httpx.get(f"http://127.0.0.1:{port}/wrong-token/")
    assert resp.status_code == 404
    assert "Not found" in resp.text


def test_different_token_on_existing_listener_raises_value_error():
    port = _free_port()
    token_a = "token-conflict-aaaa"
    token_b = "token-conflict-bbbb"
    result, thread, stderr = _run_server(
        TINY_PNG, port, timeout=5.0, token=token_a
    )

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    with pytest.raises(ValueError, match="different token"):
        serve_captcha(
            TINY_PNG,
            host="127.0.0.1",
            port=port,
            token=token_b,
            timeout=0.1,
        )

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(url, data={"captcha": "x", "challenge": challenge_id})
    thread.join(timeout=5.0)


def test_form_page_includes_challenge_hidden_field():
    port = _free_port()
    token = "hidden-field-token1"
    result, thread, stderr = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    page = httpx.get(url)
    assert page.status_code == 200
    assert '<input type="hidden" name="challenge"' in page.text

    challenge_id = _challenge_id_from_page(page)
    assert challenge_id.isdigit()

    httpx.post(url, data={"captcha": "x", "challenge": challenge_id})
    thread.join(timeout=5.0)


def test_post_missing_challenge_id_is_discarded():
    port = _free_port()
    token = "missing-id-token01"
    result, thread, stderr = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    # Submit without the challenge identifier.
    resp = httpx.post(url, data={"captcha": "answer"})
    assert resp.status_code == 200
    assert "fue descartada" in resp.text.lower()
    assert thread.is_alive()

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(url, data={"captcha": "real", "challenge": challenge_id})
    thread.join(timeout=5.0)
    assert result.get("answer") == "real"


def test_stale_post_after_timeout_is_discarded_and_new_challenge_unanswered():
    port = _free_port()
    token = "stale-token-00001"
    result1, thread1, stderr1 = _run_server(
        TINY_PNG, port, timeout=0.5, token=token
    )

    url = _wait_for_url(stderr1, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    page1 = httpx.get(url)
    challenge_id_a = _challenge_id_from_page(page1)
    thread1.join(timeout=2.0)
    assert isinstance(result1.get("exc"), CaptchaTimeoutError)

    # Register challenge B on the same listener.
    result2, thread2, _stderr2 = _run_server(
        TINY_PNG, port, timeout=5.0, token=token
    )
    page2 = httpx.get(url)
    challenge_id_b = _challenge_id_from_page(page2)
    assert challenge_id_b != challenge_id_a

    # POST the stale answer for challenge A.
    resp = httpx.post(
        url,
        data={"captcha": "stale answer", "challenge": challenge_id_a},
    )
    assert resp.status_code == 200
    assert "fue descartada" in resp.text.lower()
    assert thread2.is_alive(), "live challenge B must still be pending"

    # Challenge B remains answerable with its own identifier.
    httpx.post(
        url,
        data={"captcha": "correct", "challenge": challenge_id_b},
    )
    thread2.join(timeout=5.0)
    assert result2.get("answer") == "correct"


def test_stale_page_does_not_claim_nothing_pending_during_live_challenge():
    port = _free_port()
    token = "stale-msg-token01"
    result, thread, stderr = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)

    resp = httpx.post(
        url,
        data={"captcha": "x", "challenge": str(int(challenge_id) + 1)},
    )
    assert resp.status_code == 200
    assert "fue descartada" in resp.text.lower()
    assert "No hay ningún captcha pendiente" not in resp.text

    httpx.post(url, data={"captcha": "x", "challenge": challenge_id})
    thread.join(timeout=5.0)


def test_stop_without_start_releases_listening_socket():
    port = _free_port()
    token = "stop-no-start-01"
    server = CaptchaServer(("127.0.0.1", port), token=token)
    assert not _port_is_free("127.0.0.1", port)
    server.stop()
    assert _port_is_free("127.0.0.1", port)


def test_url_is_not_announced_for_busy_challenge():
    port = _free_port()
    token = "busy-announce-token1"
    result, thread, stderr = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"

    stderr2 = io.StringIO()
    with pytest.raises(CaptchaBusyError):
        with contextlib.redirect_stderr(stderr2):
            serve_captcha(
                TINY_PNG, host="127.0.0.1", port=port, token=token, timeout=0.1
            )
    assert "http://" not in stderr2.getvalue()

    page = httpx.get(url)
    challenge_id = _challenge_id_from_page(page)
    httpx.post(url, data={"captcha": "x", "challenge": challenge_id})
    thread.join(timeout=5.0)


def test_stable_token_is_persisted_and_reused(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    token = _token_from_url(url)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)

    # Shut down the real server cleanly so the test thread does not hang.
    page = httpx.get(f"http://127.0.0.1:{port}/{token}/")
    challenge_id = _challenge_id_from_page(page)
    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "x", "challenge": challenge_id},
    )
    thread.join(timeout=5.0)

    token_file = tmp_path / "state" / "navaja" / "captcha-token"
    assert token_file.exists()
    assert token_file.read_text(encoding="utf-8") == token


def test_secrets_token_urlsafe_is_not_patched():
    original = secrets.token_urlsafe
    with pytest.raises(CaptchaTimeoutError):
        serve_captcha(TINY_PNG, token="valid-token-12345", timeout=0.1)
    assert secrets.token_urlsafe is original


# --- Host resolution tests --------------------------------------------------


def test_resolve_host_env_wins(monkeypatch):
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", "100.64.0.1")
    host, reason = resolve_captcha_host()
    assert host == "100.64.0.1"
    assert reason == "NAVAJA_CAPTCHA_HOST"


def test_resolve_host_prefers_tailscale0(monkeypatch):
    monkeypatch.delenv("NAVAJA_CAPTCHA_HOST", raising=False)
    interfaces = [
        ("eth0", socket.AF_INET, "10.0.0.5"),
        ("tailscale0", socket.AF_INET, "100.64.0.2"),
        ("lo", socket.AF_INET, "127.0.0.1"),
    ]
    host, reason = resolve_captcha_host(interfaces)
    assert host == "100.64.0.2"
    assert "tailscale0" in reason


def test_resolve_host_ignores_tailscale0_ipv6():
    interfaces = [
        ("tailscale0", socket.AF_INET6, "fd7a:115c:a1e0::1"),
    ]
    host, reason = resolve_captcha_host(interfaces)
    assert host == "127.0.0.1"


def test_resolve_host_falls_back_to_loopback(monkeypatch):
    monkeypatch.delenv("NAVAJA_CAPTCHA_HOST", raising=False)
    interfaces = [("eth0", socket.AF_INET, "10.0.0.5")]
    host, reason = resolve_captcha_host(interfaces)
    assert host == "127.0.0.1"
    assert "no tailscale0" in reason


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "::", "   "])
def test_resolve_host_rejects_all_interfaces(bad_host, monkeypatch):
    monkeypatch.setenv("NAVAJA_CAPTCHA_HOST", bad_host)
    with pytest.raises(ValueError, match="refusing to bind"):
        resolve_captcha_host()


def test_imports_without_fcntl_and_degrades_to_loopback():
    """Simulate Windows: fcntl is absent, but the package still imports."""
    script = """
import sys

class _FcntlBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("No module named 'fcntl'")
        return None

sys.meta_path.insert(0, _FcntlBlocker())
import navaja
print(navaja.captcha.resolve_captcha_host())
"""
    env = os.environ.copy()
    env.pop("NAVAJA_CAPTCHA_HOST", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == repr(("127.0.0.1", "no tailscale0 interface found"))


def test_interface_ipv4_short_circuits_without_fcntl(monkeypatch):
    monkeypatch.delenv("NAVAJA_CAPTCHA_HOST", raising=False)
    monkeypatch.setattr(captcha, "fcntl", None)
    assert captcha._interface_ipv4("tailscale0") is None


def test_resolve_host_falls_back_to_loopback_without_fcntl(monkeypatch):
    monkeypatch.delenv("NAVAJA_CAPTCHA_HOST", raising=False)
    monkeypatch.setattr(captcha, "fcntl", None)
    host, reason = resolve_captcha_host()
    assert host == "127.0.0.1"
    assert reason == "no tailscale0 interface found"


# --- Token resolution tests -------------------------------------------------


def test_resolve_token_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", "valid-token-123456")
    token, reason = resolve_captcha_token(token_path=tmp_path / "token")
    assert token == "valid-token-123456"
    assert reason == "NAVAJA_CAPTCHA_TOKEN"


def test_resolve_token_reuses_persisted_token(monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    path = tmp_path / "token"
    path.write_text("persisted-token-123", encoding="utf-8")
    token1, reason1 = resolve_captcha_token(token_path=path)
    token2, reason2 = resolve_captcha_token(token_path=path)
    assert token1 == token2 == "persisted-token-123"
    assert "persisted" in reason1
    assert "persisted" in reason2


def test_resolve_token_generates_and_persists(monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    path = tmp_path / "captcha-token"
    token, reason = resolve_captcha_token(token_path=path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == token
    # File modes are POSIX-specific: Windows reports regular files as 0o666
    # and ignores mkdir(mode=...).
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)
    assert "generated" in reason


def test_resolve_token_persists_when_fchmod_unavailable(monkeypatch, tmp_path):
    """Simulate Windows: os.fchmod is absent, but token generation still works."""
    if hasattr(os, "fchmod"):
        monkeypatch.delattr(os, "fchmod")
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    path = tmp_path / "captcha-token"
    token, reason = resolve_captcha_token(token_path=path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == token
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)
    assert "generated" in reason


def test_persist_token_retries_transient_move_failure(monkeypatch, tmp_path):
    """The move retries on a Windows-style access-denied race and succeeds."""
    calls: list[tuple[str, str]] = []
    original_replace = os.replace

    def flaky_replace(src: str, dst: str) -> None:
        calls.append((src, dst))
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        original_replace(src, dst)

    monkeypatch.setattr("navaja.captcha.os.replace", flaky_replace)
    path = tmp_path / "captcha-token"
    token = "a-token-that-must-be-persisted-fully"
    captcha._persist_token(token, path)

    assert len(calls) >= 2
    assert path.read_text(encoding="utf-8") == token
    assert not any(
        name.startswith(".captcha-token-") for name in os.listdir(path.parent)
    )


def test_persist_token_raises_after_exhausted_retries_and_cleans_up(
    monkeypatch, tmp_path
):
    """A genuine move failure is propagated and the temp file is removed."""
    calls: list[tuple[str, str]] = []

    def always_fail(src: str, dst: str) -> None:
        calls.append((src, dst))
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr("navaja.captcha.os.replace", always_fail)
    path = tmp_path / "captcha-token"
    token = "a-token-that-must-not-be-left-behind"
    with pytest.raises(PermissionError):
        captcha._persist_token(token, path)

    assert len(calls) == captcha._TOKEN_REPLACE_MAX_ATTEMPTS
    assert not any(
        name.startswith(".captcha-token-") for name in os.listdir(path.parent)
    )


@pytest.mark.parametrize(
    "bad_content",
    ["short", "has space", "has/slash", "has%percent", "", "   "],
)
def test_resolve_token_replaces_malformed_file(bad_content, monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    path = tmp_path / "token"
    path.write_text(bad_content, encoding="utf-8")
    token, reason = resolve_captcha_token(token_path=path)
    assert token != bad_content.strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", token)
    assert "generated" in reason


def test_resolve_token_concurrent_write_is_safe(monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    path = tmp_path / "token"
    results: list[tuple[str, str]] = []

    def resolve() -> None:
        results.append(resolve_captcha_token(token_path=path))

    t1 = threading.Thread(target=resolve)
    t2 = threading.Thread(target=resolve)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    token = path.read_text(encoding="utf-8")
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", token)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{16,}", t) for t, _ in results)


def test_resolve_token_honours_xdg_state_home(monkeypatch, tmp_path):
    state_home = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.delenv("NAVAJA_CAPTCHA_TOKEN", raising=False)
    token, _reason = resolve_captcha_token()
    expected_path = state_home / "navaja" / "captcha-token"
    assert expected_path.exists()
    assert expected_path.read_text(encoding="utf-8") == token


def test_generated_token_satisfies_allowed_charset_and_length(tmp_path):
    # Run enough times to be confident the generator never strips below length.
    for i in range(20):
        path = tmp_path / f"token-{i}"
        token, _reason = resolve_captcha_token(token_path=path)
        assert len(token) >= 16
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
