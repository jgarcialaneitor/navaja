"""Full-text retrieval helpers for CENDOJ resolutions.

These are pure, client-independent utilities used by
:class:`navaja.cendoj.CendojClient` for its human-in-the-loop full-text fetch.
This module deliberately does NOT implement any automatic captcha solver.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
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
    """32-character lowercase hex hash."""

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
    r"^/search/AN/openDocument/([0-9a-f]{32})/(\d{8})/?\Z"
)


def parse_document_url(url: str) -> DocumentRef:
    """Parse a CENDOJ ``openDocument`` URL into a :class:`DocumentRef`.

    Raises:
        ValueError: if the URL does not match the expected shape.
    """
    parsed = urlparse(url)
    if parsed.hostname != "www.poderjudicial.es":
        raise ValueError(f"unexpected host: {parsed.hostname!r}")

    match = _DOCUMENT_PATH_RE.match(parsed.path)
    if not match:
        raise ValueError(
            "URL path must be /search/AN/openDocument/<32-hex-hash>/<YYYYMMDD>"
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

