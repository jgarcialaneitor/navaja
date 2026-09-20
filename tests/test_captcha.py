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
import threading
import time
from typing import Any

import httpx
import pytest

from navaja.captcha import (
    CaptchaTimeoutError,
    resolve_captcha_host,
    resolve_captcha_token,
    serve_captcha,
)

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


def test_serves_image_at_token_path_and_returns_answer():
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    token = _token_from_url(url)

    page = httpx.get(url)
    assert page.status_code == 200
    assert "<form" in page.text

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
        data={"captcha": "abc 123"},
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
        data={"captcha": "x"},
    )
    assert resp.status_code == 404

    # Shut down the real server cleanly so the test thread does not hang.
    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "x"},
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

    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "solved"},
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


def test_stable_token_is_persisted_and_reused(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    port = _free_port()
    result, thread, stderr = _run_server(TINY_PNG, port)

    url = _wait_for_url(stderr, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    token = _token_from_url(url)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)

    # Shut down the real server cleanly so the test thread does not hang.
    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "x"},
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
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)
    assert "generated" in reason


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
