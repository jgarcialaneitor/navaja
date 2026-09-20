"""Local human-in-the-loop captcha answer form.

This module deliberately does NOT implement any automatic captcha solver, OCR,
ML model, vision call or third-party captcha-solving service. A human reads the
image in a browser and types the answer.
"""

from __future__ import annotations

import html
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


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
        token: str | None = None,
    ) -> None:
        self.token = token if token is not None else secrets.token_urlsafe(32)
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
            a random 32-byte URL-safe token is generated. Supplied tokens
            must be non-empty, at least 16 characters long, and contain only
            ``A-Z``, ``a-z``, ``0-9``, ``-`` and ``_``. The token value is
            never logged or echoed in error messages.

    Returns:
        The stripped answer as typed by the human. The returned value is a
        :class:`CaptchaAnswer` (a ``str`` subclass) with ``port`` and ``url``
        attributes so the caller can build or log the local form URL.

    Raises:
        ValueError: if ``host`` would bind all network interfaces, or if
            ``token`` fails validation.
        CaptchaTimeoutError: if no answer is received within ``timeout``.
    """
    if not host or host in {"0.0.0.0", "::"}:
        raise ValueError(
            f"refusing to bind captcha server to {host!r}: "
            "that would expose the service to all network interfaces"
        )

    _validate_token(token)

    server = _CaptchaServer((host, port), image_png, timeout, _Handler, token=token)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/{server.token}/"

    print(f"Captcha form ready at {url}", flush=True)
    server.start_timeout()

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        server.answered.wait()
    finally:
        server.shutdown()
        thread.join(timeout=5.0)

    if server.answer is None:
        raise CaptchaTimeoutError(
            f"no captcha answer received within {timeout} seconds"
        )

    return CaptchaAnswer._create(server.answer, port=actual_port, url=url)
