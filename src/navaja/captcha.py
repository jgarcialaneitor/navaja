"""Local human-in-the-loop captcha answer form.

This module deliberately does NOT implement any automatic captcha solver, OCR,
ML model, vision call or third-party captcha-solving service. A human reads the
image in a browser and types the answer.
"""

from __future__ import annotations

import atexit
import contextlib
import errno
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
from collections.abc import Callable
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
    never logged or echoed in error messages.

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


class CaptchaBusyError(RuntimeError):
    """Raised when a captcha challenge is already pending on the listener."""


class _Challenge:
    """A single captcha challenge slot.

    The first answer wins. The slot is reset per challenge instead of
    latched once per server instance.
    """

    __slots__ = ("challenge_id", "image_png", "event", "answer", "done")

    def __init__(self, challenge_id: int, image_png: bytes) -> None:
        self.challenge_id = challenge_id
        self.image_png = image_png
        self.event = threading.Event()
        self.answer: str | None = None
        self.done = False


class CaptchaServer(HTTPServer):
    """Long-lived captcha form listener.

    One ``CaptchaServer`` is bound to a single ``(host, port)`` address and
    reused for every challenge in the process. It keeps the socket open until
    :meth:`stop` is called.
    """

    token: str
    _challenge: _Challenge | None
    _challenge_id: int
    _closed: bool
    _lock: threading.Lock
    _thread: threading.Thread | None

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        token: str,
    ) -> None:
        self.token = token
        self._challenge = None
        self._challenge_id = 0
        self._closed = False
        self._lock = threading.Lock()
        self._thread = None
        super().__init__(server_address, _Handler)

    @property
    def url(self) -> str:
        """The stable form URL for this listener."""
        host, port = self.server_address
        return f"http://{host}:{port}/{self.token}/"

    def start(self) -> None:
        """Start ``serve_forever`` on a daemon thread; idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self.serve_forever, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the listener and release its socket; idempotent.

        Safe to call from any thread except the serving thread itself.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread

        # Calling shutdown() from the serving thread would deadlock because it
        # waits for serve_forever() to return on the same thread.
        if thread is not None and thread is not threading.current_thread():
            try:
                self.shutdown()
            except Exception as exc:
                print(
                    f"Captcha server stop warning: shutdown failed: {exc}",
                    file=sys.stderr,
                )
            thread.join(timeout=5.0)

        try:
            self.server_close()
        except Exception as exc:
            # Do not let a socket-close failure hide a leak, but keep the
            # shutdown path non-throwing.
            print(
                f"Captcha server stop warning: could not close socket: {exc}",
                file=sys.stderr,
            )

        with self._lock:
            self._thread = None

    def is_pending(self) -> bool:
        """Return whether a challenge is currently waiting for an answer."""
        with self._lock:
            return self._challenge is not None and not self._challenge.done

    def current_image(self) -> bytes | None:
        """Return the current challenge image, or ``None`` when idle."""
        with self._lock:
            if self._challenge is None or self._challenge.done:
                return None
            return self._challenge.image_png

    def current_challenge_id(self) -> int | None:
        """Return the id of the pending challenge, or ``None`` when idle."""
        with self._lock:
            if self._challenge is None or self._challenge.done:
                return None
            return self._challenge.challenge_id

    def challenge(
        self,
        image_png: bytes,
        timeout: float,
        *,
        on_registered: Callable[[], None] | None = None,
    ) -> CaptchaAnswer:
        """Register a new challenge and block until it is answered or expires.

        Args:
            on_registered: optional callback invoked after the challenge slot
                has been registered but before blocking. Useful for announcing
                the form URL only for challenges that actually exist.

        Raises:
            CaptchaBusyError: if a challenge is already pending on this
                listener.
            CaptchaTimeoutError: if no answer arrives within ``timeout``.
        """
        with self._lock:
            if self._challenge is not None and not self._challenge.done:
                raise CaptchaBusyError(
                    "a captcha challenge is already pending on this listener"
                )
            self._challenge_id += 1
            challenge = _Challenge(self._challenge_id, image_png)
            self._challenge = challenge

        if on_registered is not None:
            on_registered()

        answered = False
        try:
            answered = challenge.event.wait(timeout)
        finally:
            with self._lock:
                if self._challenge is challenge:
                    self._challenge = None

        if not answered:
            exc = CaptchaTimeoutError(
                f"no captcha answer received within {timeout} seconds"
            )
            exc.url = self.url
            raise exc

        with self._lock:
            answer = challenge.answer

        if answer is None:
            exc = CaptchaTimeoutError(
                f"no captcha answer received within {timeout} seconds"
            )
            exc.url = self.url
            raise exc

        return CaptchaAnswer._create(
            answer, port=self.server_address[1], url=self.url
        )

    def finish(self, answer: str | None = None) -> bool:
        """Deliver an answer for the pending challenge.

        Returns ``True`` when a challenge was pending and accepted the answer,
        ``False`` when no challenge was pending (for example, a stale POST).
        """
        with self._lock:
            challenge = self._challenge
            if challenge is None or challenge.done:
                return False
            challenge.done = True
            challenge.answer = answer
            challenge.event.set()
        return True


def _form_page(token: str, challenge_id: int) -> bytes:
    image_url = f"/{token}/captcha.png"
    page = f"""<!DOCTYPE html>
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
<input type="hidden" name="challenge" value="{challenge_id}">
<input type="text" name="captcha" autocomplete="off" autofocus required>
<button type="submit">Enviar</button>
</form>
</body>
</html>"""
    return page.encode("utf-8")


def _idle_page(notice: str = "") -> bytes:
    notice_html = f'<p class="notice">{html.escape(notice)}</p>\n' if notice else ""
    page = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CENDOJ captcha</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }}
.notice {{ border-left: 4px solid #999; padding-left: 1rem; color: #555; }}
</style>
</head>
<body>
<p>No hay ningún captcha pendiente en este momento.</p>
<p>Navaja está en ejecución; esta página se actualizará automáticamente cuando llegue un nuevo desafío.</p>
{notice_html}</body>
</html>"""
    return page.encode("utf-8")


def _stale_page(notice: str) -> bytes:
    page = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CENDOJ captcha</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }}
.notice {{ border-left: 4px solid #999; padding-left: 1rem; color: #555; }}
</style>
</head>
<body>
<p class="notice">{html.escape(notice)}</p>
<p>Si hay un captcha pendiente, la página se actualizará automáticamente.</p>
</body>
</html>"""
    return page.encode("utf-8")


_ACK_PAGE = b"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CENDOJ captcha</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
</style>
</head>
<body>
<p>Respuesta recibida.</p>
<p>Si llega otro captcha, esta p&aacute;gina se actualizar&aacute; autom&aacute;ticamente.</p>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    server: CaptchaServer

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
            image = self.server.current_image()
            if image is None:
                self._send_404()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)
            return

        challenge_id = self.server.current_challenge_id()
        if challenge_id is not None:
            body = _form_page(token, challenge_id)
        else:
            body = _idle_page()
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
        submitted_challenge_id: str | None = None
        for pair in body_text.split("&"):
            if "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            if key == "captcha":
                answer = html.unescape(value.replace("+", " ")).strip()
            elif key == "challenge":
                submitted_challenge_id = value

        current_id = self.server.current_challenge_id()
        if current_id is None:
            response = _idle_page(
                notice="El formulario se envió pero no había ningún captcha pendiente."
            )
        elif (
            submitted_challenge_id is None
            or submitted_challenge_id != str(current_id)
        ):
            response = _stale_page(
                notice="La respuesta corresponde a un captcha anterior y fue descartada."
            )
        else:
            # ``finish`` can still refuse: the challenge may have expired
            # between the id check above and this call. Report that honestly
            # instead of acknowledging an answer nobody received.
            if self.server.finish(answer):
                response = _ACK_PAGE
            else:
                response = _stale_page(
                    notice=(
                        "La respuesta llegó cuando el captcha ya había vencido "
                        "y fue descartada."
                    )
                )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


# Process-wide registry of long-lived captcha listeners, keyed by the
# requested (host, port) pair. ``port=0`` creates a single ephemeral listener
# per host that is reused for the rest of the process.
_captcha_server_lock = threading.Lock()
_captcha_servers: dict[tuple[str, int], CaptchaServer] = {}


def _get_or_create_captcha_server(
    host: str,
    port: int,
    token: str,
) -> CaptchaServer:
    """Return the shared listener for ``(host, port)``, creating it if needed.

    Raises:
        ValueError: if a listener already exists for this address but with a
            different token.
        RuntimeError: if the address is held by a foreign process.
    """
    key = (host, port)
    with _captcha_server_lock:
        server = _captcha_servers.get(key)
        if server is not None:
            if server.token != token:
                raise ValueError(
                    f"captcha server already exists for {host!r} port {port} "
                    "with a different token"
                )
            return server

        try:
            server = CaptchaServer((host, port), token=token)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    f"captcha server cannot bind to {host!r} port {port}: "
                    "address already in use"
                ) from exc
            raise

        _captcha_servers[key] = server
        server.start()
        return server


def stop_shared_captcha_server() -> None:
    """Stop every shared captcha listener and clear the registry."""
    with _captcha_server_lock:
        servers = list(_captcha_servers.values())
        _captcha_servers.clear()
    for server in servers:
        server.stop()


atexit.register(stop_shared_captcha_server)


def start_shared_captcha_server(host: str, port: int, token: str) -> str:
    """Start the shared captcha listener for ``(host, port)``.

    The listener is created if it does not already exist. If it already
    exists, the existing listener is returned as long as ``token`` matches.

    Args:
        host: network interface to bind. ``0.0.0.0``, ``::`` and empty
            values are rejected with ``ValueError``.
        port: TCP port to bind.
        token: URL-path token for the captcha form. Must be non-empty, at
            least 16 characters long, and contain only ``A-Z``, ``a-z``,
            ``0-9``, ``-`` and ``_``.

    Returns:
        The stable form URL for the listener.

    Raises:
        ValueError: if ``host`` or ``token`` fail validation, or if a
            listener already exists for this address with a different token.
        RuntimeError: if the address is held by a foreign process.
    """
    host = _validate_host(host)
    _validate_token(token)
    server = _get_or_create_captcha_server(host, port, token)
    return server.url


def is_shared_captcha_server_running(host: str, port: int) -> bool:
    """Return whether a shared captcha listener is running on ``(host, port)``."""
    key = (host, port)
    with _captcha_server_lock:
        server = _captcha_servers.get(key)
        if server is None:
            return False
        thread = server._thread
        return thread is not None and thread.is_alive()


def serve_captcha(
    image_png: bytes,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    timeout: float = 300.0,
    token: str | None = None,
) -> CaptchaAnswer:
    """Show the captcha image to a human and return the typed answer.

    The underlying HTTP listener is created once per ``(host, port)`` address
    and reused for the lifetime of the process. ``port=0`` therefore creates a
    single ephemeral listener for ``host`` on first use; the announced URL
    stays stable afterwards. The listener is stopped by
    :func:`stop_shared_captcha_server`, which is also registered with
    :mod:`atexit`.

    Args:
        image_png: raw PNG bytes to display.
        host: network interface to bind. Defaults to IPv4 loopback. Tailnet
            users may pass a private Tailnet IP such as ``100.x.y.z``.
        port: TCP port; ``0`` lets the OS choose a free port once, and the
            same port is reused for every later challenge on this ``host``.
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
        ValueError: if ``host`` would bind all network interfaces, if
            ``token`` fails validation, or if the requested address already has
            a listener with a different token.
        RuntimeError: if the requested address is already in use by a foreign
            process.
        CaptchaBusyError: if a challenge is already pending on this listener.
        CaptchaTimeoutError: if no answer is received within ``timeout``.
    """
    host = _validate_host(host)

    if token is None:
        token, _token_reason = resolve_captcha_token()
    else:
        _validate_token(token)

    server = _get_or_create_captcha_server(host, port, token)
    url = server.url

    def _announce() -> None:
        print(
            f"Captcha form binding to {host}; ready at {url}",
            file=sys.stderr,
            flush=True,
        )

    return server.challenge(image_png, timeout, on_registered=_announce)
