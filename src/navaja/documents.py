"""Full-text retrieval helpers for CENDOJ resolutions.

These are pure, client-independent utilities used by
:class:`navaja.cendoj.CendojClient` for its human-in-the-loop full-text fetch.
This module deliberately does NOT implement any automatic captcha solver.
"""

from __future__ import annotations

import errno
import io
import os
import re
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.poderjudicial.es"
INDEX_URL = f"{BASE_URL}/search/indexAN.jsp"
CAPTCHA_IMAGE_URL = f"{BASE_URL}/search/stickyImg"
CONTENIDOS_URL = f"{BASE_URL}/search/contenidos.action"


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """Reference extracted from a CENDOJ ``openDocument`` URL."""

    reference: str
    """16- or 32-character hex hash, exactly as received."""

    optimize: str
    """YYYYMMDD optimization/index date token."""

    access_to_pdf_url: str
    """URL that requests the full text."""


@dataclass(frozen=True, slots=True)
class FullTextResult:
    """Outcome of a full-text fetch attempt.

    Fields:
        ok: whether a full text was successfully retrieved.
        content_type: the Content-Type header of the final response.
        text: extracted text (from PDF or HTML) of the final response.
        pdf_bytes: raw PDF bytes if the final response was a PDF, else None.
        attempts: number of captcha challenges presented to the human.
        requests: total HTTP requests issued (initial GET plus each captcha POST).
        error: human-readable error message when ok is False.
    """

    ok: bool
    content_type: str | None
    text: str
    pdf_bytes: bytes | None
    attempts: int
    requests: int
    error: str | None = None


class FullTextError(RuntimeError):
    """Raised when full-text retrieval cannot complete."""


_DOCUMENT_PATH_RE = re.compile(
    r"^/search/AN/openDocument/([0-9a-fA-F]{16}|[0-9a-fA-F]{32})/(\d{8})/?\Z"
)


def parse_document_url(url: str) -> DocumentRef:
    """Parse a CENDOJ ``openDocument`` URL into a :class:`DocumentRef`.

    The path must contain a 16- or 32-character hex reference followed by an
    ``YYYYMMDD`` optimization token. The reference is preserved exactly as it
    appears in the URL.

    Raises:
        ValueError: if the URL does not match the expected shape.
    """
    parsed = urlparse(url)
    if parsed.hostname != "www.poderjudicial.es":
        raise ValueError(f"unexpected host: {parsed.hostname!r}")

    match = _DOCUMENT_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError(
            "URL path must be /search/AN/openDocument/<16-or-32-hex-hash>/<YYYYMMDD>"
        )

    reference, optimize = match.groups()
    params: dict[str, str] = {
        "action": "accessToPDF",
        "publicinterface": "true",
        "tab": "AN",
        "reference": reference,
        "encode": "true",
        "optimize": optimize,
        "databasematch": "AN",
    }
    access_url = f"{CONTENIDOS_URL}?{urlencode(params)}"
    return DocumentRef(
        reference=reference,
        optimize=optimize,
        access_to_pdf_url=access_url,
    )


def _is_captcha_page(response: httpx.Response) -> bool:
    """Heuristic: did the site hand us the captcha challenge page?"""
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/pdf"):
        return False
    text = response.text.lower()
    return "stickyimg" in text or 'action="captcha"' in text or "name=\"captcha\"" in text


def _is_pdf(content_type: str | None, data: bytes) -> bool:
    return data.startswith(b"%PDF") or (
        content_type is not None and "application/pdf" in content_type
    )


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes using ``pypdf``."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return f"[PDF extraction failed: {exc}]"


def _extract_html_text(data: bytes) -> str:
    """Strip tags and collapse whitespace from HTML bytes."""
    soup = BeautifulSoup(data, "lxml")
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _extract_text(content_type: str | None, data: bytes) -> str:
    if _is_pdf(content_type, data):
        return _extract_pdf_text(data)
    return _extract_html_text(data)


def _captcha_post_data(ref: DocumentRef, answer: str) -> dict[str, str]:
    return {
        "action": "captcha",
        "prevaction": "accessToPDF",
        "publicinterface": "true",
        "tab": "AN",
        "reference": ref.reference,
        "encode": "true",
        "optimize": ref.optimize,
        "databasematch": "AN",
        "captcha": answer,
    }


def resolve_pdf_destination() -> tuple[Path, str]:
    """Resolve the directory where downloaded PDFs should be persisted.

    Resolution order:

    1. ``NAVAJA_PDF_DIR`` environment variable, if set.
    2. ``$XDG_DATA_HOME/navaja/pdfs`` when ``XDG_DATA_HOME`` is set,
       otherwise ``~/.local/share/navaja/pdfs``.

    Returns:
        A ``(path, reason)`` tuple. ``reason`` names the source of the
        decision.
    """
    env_dir = os.environ.get("NAVAJA_PDF_DIR")
    if env_dir:
        return Path(env_dir), "NAVAJA_PDF_DIR"

    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home:
        return Path(data_home) / "navaja" / "pdfs", "XDG_DATA_HOME"

    return Path.home() / ".local" / "share" / "navaja" / "pdfs", "XDG default"


def _parse_content_type_name(content_type: str | None) -> str | None:
    """Extract the ``name=`` parameter from a Content-Type header value."""
    if not content_type:
        return None
    msg = Message()
    msg["Content-Type"] = content_type
    value = msg.get_param("name")
    if isinstance(value, tuple):
        # RFC 2231 / RFC 5987 encoded parameter: (charset, language, value)
        _, _, raw = value
        return raw
    return value


def _sanitize_filename(name: str) -> str | None:
    """Return a safe basename from the server-sent ``name=`` value.

    Rejects NUL bytes, absolute paths, path separators, parent-directory
    references, leading tildes, and empty results.
    """
    if not name or "\x00" in name:
        return None
    if os.path.isabs(name):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    base = os.path.basename(name).strip()
    if not base or base in {".", ".."}:
        return None
    if base.startswith("~"):
        return None
    return base


def _ensure_pdf_extension(name: str) -> str:
    """Return ``name`` with a ``.pdf`` suffix, adding it if necessary."""
    if name.lower().endswith(".pdf"):
        return name
    return f"{name}.pdf"


def _fallback_filename(ref: DocumentRef) -> str:
    """Deterministic filename built from ``ref`` fields."""
    return f"{ref.reference}_{ref.optimize}.pdf"


def _filename_from_content_type(
    content_type: str | None,
    ref: DocumentRef,
    name_max: int,
) -> tuple[str, str]:
    """Choose a safe filename and report the reason for the choice.

    Returns:
        A ``(filename, reason)`` tuple. ``reason`` is one of
        ``server_sent_name``, ``missing_name``, ``unsafe_name``, or
        ``overlong_name``.
    """
    raw_name = _parse_content_type_name(content_type)
    if raw_name is None:
        return _fallback_filename(ref), "missing_name"
    safe = _sanitize_filename(raw_name)
    if safe is None:
        return _fallback_filename(ref), "unsafe_name"
    candidate = _ensure_pdf_extension(safe)
    if len(os.fsencode(candidate)) > name_max:
        return _fallback_filename(ref), "overlong_name"
    return candidate, "server_sent_name"


def _unique_path(dest: Path, filename: str, pdf_bytes: bytes) -> tuple[Path, str]:
    """Return a path inside ``dest`` for ``filename``, handling collisions.

    Collision strategy: if the target already exists with identical bytes,
    report it as already satisfied. If the bytes differ, insert an
    incrementing counter before the ``.pdf`` extension until a unique name
    is found.

    This helper does not catch its own filesystem errors: callers are
    expected to wrap it and convert OSError subclasses into a failed
    :class:`PdfSaveResult`.
    """
    target = dest / filename
    if not target.exists():
        return target, "written"

    existing = target.read_bytes()
    if existing == pdf_bytes:
        return target, "identical_bytes"

    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".pdf"
    counter = 1
    while True:
        candidate = dest / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate, "written"
        if candidate.read_bytes() == pdf_bytes:
            return candidate, "identical_bytes"
        counter += 1


def _filesystem_error_result(exc: OSError, path: Path) -> PdfSaveResult:
    """Map a filesystem probe failure to a coherent failure result."""
    if exc.errno == errno.ENAMETOOLONG:
        reason = "name_too_long"
    elif exc.errno == errno.EISDIR:
        reason = "directory_collision"
    else:
        reason = "write_failed"
    return PdfSaveResult(
        ok=False,
        path=None,
        reason=reason,
        error=f"Could not access {path}: {exc}",
    )


@dataclass(frozen=True, slots=True)
class PdfSaveResult:
    """Outcome of an attempt to persist PDF bytes.

    Fields:
        ok: whether the bytes were written or already present identically.
        path: the filesystem path, or None when no file was written.
        reason: short human-readable description of the outcome.
        error: human-readable error message when ok is False.
    """

    ok: bool
    path: Path | None
    reason: str
    error: str | None = None


def save_pdf(
    pdf_bytes: bytes,
    content_type: str | None,
    ref: DocumentRef,
    destination: str | os.PathLike[str],
) -> PdfSaveResult:
    """Save ``pdf_bytes`` into ``destination`` with a safe filename.

    The filename is taken from the ``name=`` parameter in ``content_type``
    when present and safe; otherwise a deterministic fallback built from
    ``ref.reference`` and ``ref.optimize`` is used. The destination
    directory is created on demand.

    Collision strategy: if the target path already exists and contains
    identical bytes, the existing file is reported as already satisfied.
    If the bytes differ, a counter suffix is inserted before ``.pdf`` until
    a unique name is found.

    Filesystem problems encountered while probing for collisions (for
    example, a directory occupying the target path, or an unreadable
    existing file) are reported as failed saves with ``ok=False`` rather
    than raised. An overlong server-sent filename is detected before any
    filesystem probe and is degraded to the deterministic fallback built
    from ``ref``; the returned ``reason`` is ``overlong_name`` so the
    caller can still tell the fallback was used.

    Args:
        pdf_bytes: the raw PDF bytes to persist.
        content_type: the Content-Type header value of the response.
        ref: the document reference used for the deterministic fallback.
        destination: directory where the file should be written.

    Returns:
        A :class:`PdfSaveResult` describing the path written or the reason
        the write failed.
    """
    if not pdf_bytes:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="empty_payload",
            error="No PDF bytes to persist",
        )

    if content_type and "application/pdf" not in content_type:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="not_pdf",
            error="Content-Type does not indicate a PDF",
        )

    dest = Path(destination)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="mkdir_failed",
            error=f"Could not create destination directory {dest}: {exc}",
        )

    try:
        name_max = os.pathconf(dest, "PC_NAME_MAX")
    except (ValueError, OSError):
        name_max = 255

    filename, name_reason = _filename_from_content_type(content_type, ref, name_max)
    candidate = dest / filename
    try:
        candidate.resolve().relative_to(dest.resolve())
    except ValueError:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="path_escape",
            error="Resolved path escapes the destination directory",
        )
    except OSError as exc:
        return _filesystem_error_result(exc, candidate)

    try:
        target, collision_reason = _unique_path(dest, filename, pdf_bytes)
    except OSError as exc:
        return _filesystem_error_result(exc, candidate)

    if collision_reason == "identical_bytes":
        return PdfSaveResult(ok=True, path=target, reason="identical_bytes")

    try:
        target.write_bytes(pdf_bytes)
    except OSError as exc:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="write_failed",
            error=f"Could not write PDF to {target}: {exc}",
        )

    return PdfSaveResult(ok=True, path=target, reason=name_reason)


def save_full_text_pdf(
    result: FullTextResult,
    ref: DocumentRef,
) -> PdfSaveResult:
    """Decide whether to persist a PDF from ``result`` and do it.

    This is the shared decision point used by every entry point that may
    leave a fetched PDF on disk. It maps the three non-save outcomes to
    their reason vocabulary and narrows the defensive ``resolve_failed``
    guard so that only a failure in :func:`resolve_pdf_destination` is
    reported as a destination-resolution failure.

    Args:
        result: the outcome of the full-text fetch.
        ref: the parsed document reference (already known to the caller).

    Returns:
        A :class:`PdfSaveResult`. Reasons produced here are:
        ``not_attempted`` when ``result.ok`` is ``False``,
        ``not_pdf`` when the response was not a PDF, and
        ``resolve_failed`` when the destination directory cannot be
        resolved. All other reasons come from :func:`save_pdf`.
    """
    if not result.ok:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="not_attempted",
            error=None,
        )

    if result.pdf_bytes is None:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="not_pdf",
            error=None,
        )

    try:
        destination, _dest_reason = resolve_pdf_destination()
    except Exception as exc:
        return PdfSaveResult(
            ok=False,
            path=None,
            reason="resolve_failed",
            error=f"Could not resolve PDF destination: {exc}",
        )

    return save_pdf(
        result.pdf_bytes,
        result.content_type,
        ref,
        destination,
    )

