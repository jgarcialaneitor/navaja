"""Tests for the ``navaja-doc`` CLI.

Everything here is offline and deterministic: no test contacts the live
CENDOJ site. The CENDOJ client is always substituted, and on the fail-fast
path any use of it is recorded so the test can prove, structurally, that no
request was attempted.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator

import pytest

from navaja import cli
from navaja.captcha import CaptchaServer
from navaja.documents import FullTextResult


TOKEN = "cli-test-token-0123456789"
HOST = "127.0.0.1"
DOCUMENT_URL = (
    "https://www.poderjudicial.es/search/AN/openDocument/"
    "0123456789abcdef/20240101"
)


@pytest.fixture(autouse=True)
def _isolated_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Pin the captcha token and keep the state file out of the real home."""
    monkeypatch.setenv("NAVAJA_CAPTCHA_TOKEN", TOKEN)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def _free_port(host: str = HOST) -> int:
    """Return a port the OS reports as free right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _navaja_listener(host: str = HOST) -> Iterator[int]:
    """Hold a port with a real navaja captcha listener."""
    server = CaptchaServer((host, 0), token=TOKEN)
    try:
        server.start()
        yield int(server.server_address[1])
    finally:
        server.stop()


@contextlib.contextmanager
def _foreign_listener(reply: bytes | None, host: str = HOST) -> Iterator[int]:
    """Hold a port with a plain socket that is not navaja.

    ``reply`` is the raw payload sent back to a connecting client; ``None``
    keeps the connection open without answering anything at all.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            with conn:
                if reply is None:
                    # Hold the connection past the CLI's confirmation timeout.
                    stop.wait(cli._CONFIRM_TIMEOUT + 1.0)
                    continue
                with contextlib.suppress(OSError):
                    conn.recv(4096)
                    conn.sendall(reply)

    thread = threading.Thread(target=_serve, daemon=True)
    try:
        sock.bind((host, 0))
        sock.listen(5)
        sock.settimeout(0.2)
        port = int(sock.getsockname()[1])
        thread.start()
        yield port
    finally:
        stop.set()
        sock.close()
        thread.join(timeout=5.0)


_NOT_NAVAJA_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"Content-Length: 14\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"some other app"
)


def _forbidden_client(uses: list[str]) -> type:
    """Build a CendojClient stand-in whose every use is recorded."""

    class _ForbiddenClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            uses.append("constructed")

        def __enter__(self) -> "_ForbiddenClient":
            uses.append("entered")
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def __getattr__(self, name: str) -> object:
            uses.append(name)
            raise AssertionError(f"the CLI must not use the client ({name})")

    return _ForbiddenClient


def _recording_client(calls: list[dict[str, object]], result: FullTextResult) -> type:
    """Build a CendojClient stand-in that records the fetch and answers offline."""

    class _RecordingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append({"event": "constructed"})

        def __enter__(self) -> "_RecordingClient":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def fetch_full_text(self, url: str, **kwargs: object) -> FullTextResult:
            calls.append({"event": "fetch_full_text", "url": url, **kwargs})
            return result

    return _RecordingClient


def _ok_result() -> FullTextResult:
    return FullTextResult(
        ok=True,
        content_type="text/html; charset=utf-8",
        text="texto completo",
        pdf_bytes=None,
        attempts=1,
        requests=2,
    )


def test_occupied_port_fails_fast_without_contacting_cendoj(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uses: list[str] = []
    monkeypatch.setattr(cli, "CendojClient", _forbidden_client(uses))

    with _foreign_listener(_NOT_NAVAJA_RESPONSE) as port:
        exit_code = cli.main(
            [DOCUMENT_URL, "--host", HOST, "--port", str(port)]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert uses == []
    assert "--port 0" in captured.err
    assert "stop the process holding the port" in captured.err


def test_conflict_message_names_navaja_when_occupant_is_navaja(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uses: list[str] = []
    monkeypatch.setattr(cli, "CendojClient", _forbidden_client(uses))

    with _navaja_listener() as port:
        exit_code = cli.main(
            [DOCUMENT_URL, "--host", HOST, "--port", str(port)]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert uses == []
    assert "navaja session is already holding" in captured.err
    assert str(port) in captured.err
    assert "--port 0" in captured.err


def test_conflict_message_is_generic_for_a_foreign_listener(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uses: list[str] = []
    monkeypatch.setattr(cli, "CendojClient", _forbidden_client(uses))

    with _foreign_listener(_NOT_NAVAJA_RESPONSE) as port:
        exit_code = cli.main(
            [DOCUMENT_URL, "--host", HOST, "--port", str(port)]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert uses == []
    assert "is in use by another process" in captured.err
    assert "navaja session" not in captured.err
    assert "--port 0" in captured.err


def test_conflict_message_is_generic_when_occupant_never_answers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    uses: list[str] = []
    monkeypatch.setattr(cli, "CendojClient", _forbidden_client(uses))

    with _foreign_listener(None) as port:
        exit_code = cli.main(
            [DOCUMENT_URL, "--host", HOST, "--port", str(port)]
        )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert uses == []
    assert "is in use by another process" in captured.err


def test_conflict_message_never_leaks_the_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "CendojClient", _forbidden_client([]))

    with _navaja_listener() as port:
        assert cli.main([DOCUMENT_URL, "--host", HOST, "--port", str(port)]) == 1
    navaja_output = capsys.readouterr()

    with _foreign_listener(_NOT_NAVAJA_RESPONSE) as port:
        assert cli.main([DOCUMENT_URL, "--host", HOST, "--port", str(port)]) == 1
    foreign_output = capsys.readouterr()

    for captured in (navaja_output, foreign_output):
        assert TOKEN not in captured.err
        assert TOKEN not in captured.out


def test_port_zero_skips_the_preflight_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[str, int]] = []

    def _spy(host: str, port: int) -> bool:
        probes.append((host, port))
        return False

    monkeypatch.setattr(cli, "_address_occupied", _spy)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "CendojClient", _recording_client(calls, _ok_result()))

    exit_code = cli.main([DOCUMENT_URL, "--host", HOST, "--port", "0"])

    assert exit_code == 0
    assert probes == []
    assert calls[-1]["event"] == "fetch_full_text"


def test_free_port_proceeds_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli, "CendojClient", _recording_client(calls, _ok_result()))
    port = _free_port()

    exit_code = cli.main([DOCUMENT_URL, "--host", HOST, "--port", str(port)])

    captured = capsys.readouterr()
    assert exit_code == 0
    fetch = calls[-1]
    assert fetch["event"] == "fetch_full_text"
    assert fetch["url"] == DOCUMENT_URL
    assert fetch["host"] == HOST
    assert fetch["port"] == port
    assert "Result: success" in captured.out


def test_late_bind_conflict_reports_the_same_guidance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The port can be taken between the probe and the real bind."""

    class _RacingClient:
        def __enter__(self) -> "_RacingClient":
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def fetch_full_text(self, url: str, **kwargs: object) -> FullTextResult:
            raise RuntimeError(
                f"captcha server cannot bind to {HOST!r} port {kwargs['port']}: "
                "address already in use"
            )

    monkeypatch.setattr(cli, "CendojClient", _RacingClient)
    port = _free_port()

    exit_code = cli.main([DOCUMENT_URL, "--host", HOST, "--port", str(port)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "address already in use" not in captured.err
    assert "is in use by another process" in captured.err
    assert "--port 0" in captured.err
