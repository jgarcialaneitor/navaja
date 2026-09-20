"""Offline tests for CENDOJ full-text retrieval.

These tests use ``httpx.MockTransport`` and a patched ``serve_captcha`` so they
never hit the live site and never block for a human.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from navaja.captcha import CaptchaAnswer
from navaja.cendoj import BASE_URL, CendojClient, INDEX_URL
from navaja.documents import (
    DocumentRef,
    FullTextResult,
    parse_document_url,
)

REFERENCE = "aabbccddeeff00112233445566778899"
OPTIMIZE = "20260911"
DOC_URL = (
    f"https://www.poderjudicial.es/search/AN/openDocument/{REFERENCE}/{OPTIMIZE}"
)
PDF_BYTES = b"%PDF-1.4 fake pdf content"
HTML_DOC = b"<html><body><p>This is the full text.</p></body></html>"
CAPTCHA_HTML = """\
<html>
<body>
<img src="stickyImg">
<form action="contenidos.action" method="POST">
<input type="hidden" name="action" value="captcha">
<input type="text" name="captcha">
</form>
</body>
</html>
"""


def _assert_access_url(ref: DocumentRef) -> None:
    parsed = urlparse(ref.access_to_pdf_url)
    assert parsed.path == "/search/contenidos.action"
    params = parse_qs(parsed.query)
    assert params["action"] == ["accessToPDF"]
    assert params["publicinterface"] == ["true"]
    assert params["tab"] == ["AN"]
    assert params["reference"] == [REFERENCE]
    assert params["encode"] == ["true"]
    assert params["optimize"] == [OPTIMIZE]
    assert params["databasematch"] == ["AN"]


def test_parse_document_url_accepts_real_shape():
    ref = parse_document_url(DOC_URL)
    assert isinstance(ref, DocumentRef)
    assert ref.reference == REFERENCE
    assert ref.optimize == OPTIMIZE
    _assert_access_url(ref)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search/AN/openDocument/aabbccddeeff00112233445566778899/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/short/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/GGBBCCDDEeff00112233445566778899/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/aabbccddeeff00112233445566778899",
        "https://www.poderjudicial.es/search/AN/openDocument/aabbccddeeff00112233445566778899/202609115",
    ],
)
def test_parse_document_url_rejects_malformed_urls(url: str):
    with pytest.raises(ValueError):
        parse_document_url(url)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    wrong_answers: int = 0,
    final: str = "pdf",
) -> tuple[CendojClient, list[httpx.Request], list[int]]:
    sent: list[httpx.Request] = []
    post_count: list[int] = [0]

    def fake_serve(
        image: bytes,
        *,
        host: str,
        port: int,
        timeout: float,
    ) -> CaptchaAnswer:
        assert image == b"fake-png"
        return CaptchaAnswer._create(
            "TEST123", port=port, url=f"http://{host}:{port}/token/"
        )

    monkeypatch.setattr("navaja.cendoj.serve_captcha", fake_serve)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html>session</html>")
        if "action=accessToPDF" in url:
            return httpx.Response(200, text=CAPTCHA_HTML)
        if "stickyImg" in url:
            return httpx.Response(200, content=b"fake-png")
        if request.method == "POST" and "contenidos.action" in url:
            post_count[0] += 1
            if post_count[0] <= wrong_answers:
                return httpx.Response(200, text=CAPTCHA_HTML)
            if final == "pdf":
                return httpx.Response(
                    200,
                    content=PDF_BYTES,
                    headers={"content-type": "application/pdf"},
                )
            if final == "html":
                return httpx.Response(
                    200,
                    content=HTML_DOC,
                    headers={"content-type": "text/html"},
                )
        return httpx.Response(404)

    client = CendojClient(transport=httpx.MockTransport(handler))
    return client, sent, post_count


def test_fetch_posts_captcha_form_with_all_hidden_fields(monkeypatch):
    client, sent, _post_count = _make_client(monkeypatch, wrong_answers=0, final="pdf")

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert isinstance(result, FullTextResult)
    assert result.ok is True

    posts = [r for r in sent if r.method == "POST"]
    assert len(posts) == 1
    body = posts[0].content.decode()
    assert "action=captcha" in body
    assert "prevaction=accessToPDF" in body
    assert "publicinterface=true" in body
    assert "tab=AN" in body
    assert f"reference={REFERENCE}" in body
    assert "encode=true" in body
    assert f"optimize={OPTIMIZE}" in body
    assert "databasematch=AN" in body
    assert "captcha=TEST123" in body


def test_fetch_detects_pdf_by_magic_bytes_and_populates_pdf_bytes(monkeypatch):
    client, _sent, _post_count = _make_client(monkeypatch, final="pdf")

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert result.ok is True
    assert result.content_type == "application/pdf"
    assert result.pdf_bytes == PDF_BYTES
    assert result.attempts == 1  # one human captcha attempt
    assert result.requests == 2  # GET accessToPDF + 1 captcha POST


def test_fetch_falls_back_to_html_stripping(monkeypatch):
    client, _sent, _post_count = _make_client(monkeypatch, final="html")

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert result.ok is True
    assert result.content_type == "text/html"
    assert result.pdf_bytes is None
    assert "This is the full text." in result.text
    assert "<p>" not in result.text


def test_fetch_reports_wrong_answer_then_succeeds(monkeypatch):
    client, _sent, post_count = _make_client(
        monkeypatch, wrong_answers=1, final="pdf"
    )

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert result.ok is True
    assert post_count[0] == 2
    assert result.attempts == 2  # two human captcha attempts
    assert result.requests == 3  # GET + 2 POSTs


def test_fetch_reports_failure_after_max_attempts(monkeypatch):
    client, _sent, post_count = _make_client(
        monkeypatch, wrong_answers=3, final="pdf"
    )

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert result.ok is False
    assert post_count[0] == 3
    assert result.attempts == 3  # three human captcha attempts
    assert result.requests == 4  # GET + 3 POSTs
    assert result.error is not None
    assert "captcha" in result.error.lower()


def test_fetch_skips_captcha_when_site_returns_pdf_directly(monkeypatch):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, text="<html>session</html>")
        if "action=accessToPDF" in url:
            return httpx.Response(
                200,
                content=PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(404)

    client = CendojClient(transport=httpx.MockTransport(handler))

    with client:
        result = client.fetch_full_text(DOC_URL)

    assert result.ok is True
    assert result.pdf_bytes == PDF_BYTES
    assert result.attempts == 0  # no captcha needed
    assert result.requests == 1  # single GET accessToPDF
    assert not any("stickyImg" in str(r.url) for r in sent)


def test_fetch_rejects_non_cendoj_url():
    with CendojClient() as client:
        with pytest.raises(ValueError):
            client.fetch_full_text("https://example.com/document")
