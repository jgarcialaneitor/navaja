"""Local human-in-the-loop captcha answer form.

This module deliberately does NOT implement any automatic captcha solver, OCR,
ML model, vision call or third-party captcha-solving service. A human reads the
image in a browser and types the answer.
"""

from __future__ import annotations

import contextlib
import fcntl
import html
import os
import re
import secrets
import socket
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


def _validate_host(host: str) -> str:
    """Validate and normalize a captcha bind host.

    Rejects ``0.0.0.0``, ``::`` and empty values because they would expose
    the local form to all network interfaces. The value is never echoed.
    """
    normalized = host.strip()
    if not normalized or normalized in {"0.0.0.0", "::"}:
        raise ValueError(
            f"refusing to bind captcha server to {normalized!r}: "
            "that would expose the service to all network interfaces"
        )
    return normalized


def _validate_token(token: str | None) -> None:
    """Validate a caller-supplied captcha URL-path token.

    The token is never echoed in error messages.
    """
    if token is None:
        return
    if not token.strip():
        raise ValueError("captcha token must be non-empty")
    if len(token) < 16:
        raise ValueError("captcha token must be at least 16 characters")
    if not _TOKEN_RE.match(token):
        raise ValueError(
            "captcha token contains characters outside the allowed set "
            "(A-Z, a-z, 0-9, '-' and '_')"
        )


def _interface_ipv4(name: str) -> str | None:
    """Return the IPv4 address assigned to interface ``name``, or ``None``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # SIOCGIFADDR on Linux.
        info = fcntl.ioctl(
            sock.fileno(),
            0x8915,
            struct.pack("256s", name.encode("utf-8")[:15]),
        )
        return socket.inet_ntoa(info[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def _list_network_interfaces() -> list[tuple[str, int, str]]:
    """Return local network interfaces as ``(name, family, address)`` tuples.

    The list only includes IPv4 addresses. Non-Linux platforms or any error
    enumerating interfaces returns an empty list.
    """
    results: list[tuple[str, int, str]] = []
    try:
        for _idx, name in socket.if_nameindex():
            addr = _interface_ipv4(name)
            if addr is not None:
                results.append((name, socket.AF_INET, addr))
    except OSError:
        pass
    return results


def resolve_captcha_host(
    interfaces: list[tuple[str, int, str]] | None = None,
) -> tuple[str, str]:
    """Decide which interface the captcha form should bind to.

    Resolution order:

    1. ``NAVAJA_CAPTCHA_HOST`` environment variable, if set.
    2. The IPv4 address of the interface named ``tailscale0``.
    3. ``127.0.0.1``.

    ``0.0.0.0``, ``::`` and empty values are rejected with ``ValueError``.

    Args:
        interfaces: optional list of ``(name, family, address)`` tuples to
            use instead of querying the real machine. This makes the resolver
            unit-testable without a Tailscale tailnet.

    Returns:
        A ``(host, reason)`` tuple. ``reason`` is a short human-readable
        description of why ``host`` was chosen.
    """
    if "NAVAJA_CAPTCHA_HOST" in os.environ:
        host = _validate_host(os.environ["NAVAJA_CAPTCHA_HOST"])
        return host, "NAVAJA_CAPTCHA_HOST"

    if interfaces is None:
        interfaces = _list_network_interfaces()

    for name, family, address in interfaces:
        if name == "tailscale0" and family == socket.AF_INET:
            host = _validate_host(address)
            return host, "tailscale0 interface"

    return "127.0.0.1", "no tailscale0 interface found"


def default_captcha_token_path() -> Path:
    """Return the default path for the persisted captcha token.

    Uses ``$XDG_STATE_HOME/navaja/captcha-token`` when ``XDG_STATE_HOME`` is
    set, otherwise ``~/.local/state/navaja/captcha-token``.
    """
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        base = Path(state_home)
    else:
        base = Path.home() / ".local" / "state"
    return base / "navaja" / "captcha-token"


def _generate_token() -> str:
    """Generate a captcha token containing only allowed characters.

    ``secrets.token_urlsafe`` already returns URL-safe base64, but we strip
    any unexpected character to keep the token strictly within the allowed
    set documented by :func:`serve_captcha`.
    """
    raw = secrets.token_urlsafe(32)
    token = "".join(c for c in raw if _TOKEN_RE.match(c))
    # secrets.token_urlsafe(32) is 43 characters long, so this is defensive.
    if len(token) < 16:
        return _generate_token()
    return token


def _persist_token(token: str, path: Path) -> None:
    """Persist ``token`` to ``path`` atomically with restrictive permissions.

    The parent directory is created with mode ``0700``. The token is written
    to a temporary file in the same directory, the temporary file is chmodded
    to ``0600``, and then it is moved into place with ``os.replace`` so a
    concurrent run cannot observe a partially written file.
    """
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=".captcha-token-")
    try:
        os.write(fd, token.encode("utf-8"))
        os.fchmod(fd, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            Path(temp_path).unlink()
        raise
    os.close(fd)
    os.replace(temp_path, path)
    # Ensure the final inode has 0600 even if umask or filesystem quirks
    # interfered with the temp-file mode.
    os.chmod(path, 0o600)


def resolve_captcha_token(
    token_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Resolve the captcha URL-path token, preferring a stable persisted one.

    Resolution order:

    1. ``NAVAJA_CAPTCHA_TOKEN`` environment variable, if set and valid.
    2. A token persisted in the state file.
    3. Generate a new token with :func:`secrets.token_urlsafe`, strip any
       character outside the allowed set, persist it, and use it.

    The token is stored at ``$XDG_STATE_HOME/navaja/captcha-token``
    (default ``~/.local/state/navaja/captcha-token``). The directory is
    created with mode ``0700`` and the file with mode ``0600``. Writes are
    atomic (temporary file in the same directory, then ``os.replace``) so
    concurrent runs cannot produce a torn file. The stable token means the
    form URL stays the same across server runs, which is important for MCP
    clients that may not surface stderr URLs.

    If the state file is unreadable or its contents fail validation, a new
    token is generated and the file is rewritten. The token value itself is
    never logged or echoed.

    Args:
        token_path: optional path to the state file. When ``None``, the
            default XDG state location is used.

    Returns:
        A ``(token, reason)`` tuple. ``reason`` describes where the token
        came from without exposing the token value.
    """
    env_token = os.environ.get("NAVAJA_CAPTCHA_TOKEN", "").strip()
    if env_token:
        _validate_token(env_token)
        return env_token, "NAVAJA_CAPTCHA_TOKEN"

    path = Path(token_path) if token_path is not None else default_captcha_token_path()

    try:
        token = path.read_text(encoding="utf-8").strip()
        _validate_token(token)
        return token, f"persisted token at {path}"
    except (OSError, ValueError):
        pass

    token = _generate_token()
    _persist_token(token, path)
    return token, f"generated and persisted token at {path}"


class CaptchaAnswer(str):
    """The human-typed captcha answer, with the local form URL attached."""

    port: int = 0
    url: str = ""

    @classmethod
    def _create(cls, value: str, *, port: int, url: str) -> "CaptchaAnswer":
        answer = cls(value)
        answer.port = port
        answer.url = url
        return answer


class CaptchaTimeoutError(TimeoutError):
    """Raised when the human does not answer the captcha in time."""


class _CaptchaServer(HTTPServer):
    token: str
    image_png: bytes
    answer: str | None = None
    answered: threading.Event
    timeout_seconds: float
    _timeout_timer: threading.Timer | None = None
    _finished: bool = False
    _lock: threading.Lock

    def __init__(
        self,
        server_address: tuple[str, int],
        image_png: bytes,
        timeout: float,
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        *,
        token: str,
    ) -> None:
        self.token = token
        self.image_png = image_png
        self.answered = threading.Event()
        self.timeout_seconds = timeout
        self._lock = threading.Lock()
        super().__init__(server_address, RequestHandlerClass)

    def start_timeout(self) -> None:
        self._timeout_timer = threading.Timer(
            self.timeout_seconds, self._shutdown_on_timeout
        )
        self._timeout_timer.daemon = True
        self._timeout_timer.start()

    def _shutdown_on_timeout(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.answered.set()
        self.shutdown()

    def finish(self, answer: str | None = None) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            if self._timeout_timer is not None:
                self._timeout_timer.cancel()
            self.answer = answer
            self.answered.set()
        # Shutdown blocks until serve_forever exits; run it from a helper thread
        # to avoid deadlocking inside the request handler.
        threading.Thread(target=self.shutdown, daemon=True).start()


class _Handler(BaseHTTPRequestHandler):
    server: _CaptchaServer

    def log_message(self, format: str, *args: object) -> None:
        # Keep the local form silent by default.
        return

    def _token_and_path(self) -> tuple[str, str]:
        path = self.path.rstrip("/")
        parts = path.split("/")
        token = parts[1] if len(parts) > 1 else ""
        return token, path

    def _send_404(self) -> None:
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        token, path = self._token_and_path()
        if token != self.server.token:
            self._send_404()
            return

        if path == f"/{token}/captcha.png":
            image = self.server.image_png
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return

        image_url = f"/{token}/captcha.png"
        html_page = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CENDOJ captcha</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }}
img {{ display: block; margin: 1rem 0; border: 1px solid #ccc; }}
input {{ font-size: 1.25rem; padding: 0.5rem; width: 100%; box-sizing: border-box; }}
button {{ font-size: 1rem; padding: 0.5rem 1rem; margin-top: 0.5rem; }}
</style>
</head>
<body>
<h1>Resuelve el captcha</h1>
<p>Escribe los caracteres que ves en la imagen y pulsa <strong>Enviar</strong>.</p>
<img src="{image_url}" alt="captcha">
<form method="POST" action="/{token}/">
<input type="text" name="captcha" autocomplete="off" autofocus required>
<button type="submit">Enviar</button>
</form>
</body>
</html>"""
        body = html_page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        token, path = self._token_and_path()
        if token != self.server.token or path != f"/{token}":
            self._send_404()
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            body_text = raw_body.decode("latin-1")

        answer = ""
        for pair in body_text.split("&"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            if key == "captcha":
                answer = html.unescape(value.replace("+", " ")).strip()
                break

        response = b"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><title>Recibido</title></head>
<body><p>Respuesta recibida. Pod&eacute;s cerrar esta pesta&ntilde;a.</p></body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.server.finish(answer)


def serve_captcha(
    image_png: bytes,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    timeout: float = 300.0,
    token: str | None = None,
) -> CaptchaAnswer:
    """Show the captcha image to a human and return the typed answer.

    Args:
        image_png: raw PNG bytes to display.
        host: network interface to bind. Defaults to IPv4 loopback. Tailnet
            users may pass a private Tailnet IP such as ``100.x.y.z``.
        port: TCP port; ``0`` lets the OS choose a free port.
        timeout: seconds to wait for an answer before raising.
        token: optional URL-path token for the captcha form. When ``None``,
            a stable persisted token is resolved from the environment or
            ``$XDG_STATE_HOME/navaja/captcha-token`` (default
            ``~/.local/state/navaja/captcha-token``); if none exists, one is
            generated and persisted. Supplied tokens must be non-empty, at
            least 16 characters long, and contain only ``A-Z``, ``a-z``,
            ``0-9``, ``-`` and ``_``. The token value is never logged or
            echoed in error messages.

    Returns:
        The stripped answer as typed by the human. The returned value is a
        :class:`CaptchaAnswer` (a ``str`` subclass) with ``port`` and ``url``
        attributes so the caller can build or log the local form URL.

    Raises:
        ValueError: if ``host`` would bind all network interfaces, or if
            ``token`` fails validation.
        CaptchaTimeoutError: if no answer is received within ``timeout``.
    """
    host = _validate_host(host)

    if token is None:
        token, _token_reason = resolve_captcha_token()
    else:
        _validate_token(token)

    server = _CaptchaServer((host, port), image_png, timeout, _Handler, token=token)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/{server.token}/"

    print(
        f"Captcha form binding to {host}; ready at {url}",
        file=sys.stderr,
        flush=True,
    )
    server.start_timeout()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        server.answered.wait()
    finally:
        server.shutdown()
        thread.join(timeout=5.0)

    if server.answer is None:
        exc = CaptchaTimeoutError(
            f"no captcha answer received within {timeout} seconds"
        )
        exc.url = url
        raise exc

    return CaptchaAnswer._create(server.answer, port=actual_port, url=url)
