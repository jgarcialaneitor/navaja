"""Offline tests for the local captcha answer server.

These tests never hit the live CENDOJ site. They start the local form server
on loopback, interact with it over HTTP, and verify the security boundaries.
"""

from __future__ import annotations

import contextlib
import io
import re
import secrets
import socket
import threading
import time
from typing import Any

import httpx
import pytest

from navaja.captcha import CaptchaTimeoutError, serve_captcha

TINY_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_url(stdout: io.StringIO, deadline: float) -> str | None:
    while time.monotonic() < deadline:
        text = stdout.getvalue()
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
    stdout_capture = io.StringIO()

    def target() -> None:
        with contextlib.redirect_stdout(stdout_capture):
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
    return result, thread, stdout_capture


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
    result, thread, stdout = _run_server(TINY_PNG, port)

    url = _wait_for_url(stdout, time.monotonic() + 2.0)
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
    result, thread, stdout = _run_server(TINY_PNG, port)

    url = _wait_for_url(stdout, time.monotonic() + 2.0)
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
    result, thread, stdout = _run_server(TINY_PNG, port, token=token)

    url = _wait_for_url(stdout, time.monotonic() + 2.0)
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


def test_random_token_generated_when_none_supplied():
    port = _free_port()
    result, thread, stdout = _run_server(TINY_PNG, port)

    url = _wait_for_url(stdout, time.monotonic() + 2.0)
    assert url is not None, "server did not print its form URL"
    token = _token_from_url(url)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", token)

    # Shut down the real server cleanly so the test thread does not hang.
    httpx.post(
        f"http://127.0.0.1:{port}/{token}/",
        data={"captcha": "x"},
    )
    thread.join(timeout=5.0)


def test_secrets_token_urlsafe_is_not_patched():
    original = secrets.token_urlsafe
    with pytest.raises(CaptchaTimeoutError):
        serve_captcha(TINY_PNG, token="valid-token-12345", timeout=0.1)
    assert secrets.token_urlsafe is original
