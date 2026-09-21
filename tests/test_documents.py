"""Offline tests for CENDOJ full-text retrieval.

These tests use ``httpx.MockTransport`` and a patched ``serve_captcha`` so they
never hit the live site and never block for a human.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from navaja.captcha import CaptchaAnswer
from navaja.cendoj import BASE_URL, CendojClient, INDEX_URL
from navaja.documents import (
    DocumentRef,
    FullTextResult,
    PdfSaveResult,
    parse_document_url,
    resolve_pdf_destination,
    save_full_text_pdf,
    save_pdf,
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


def test_parse_document_url_accepts_16_character_hash():
    reference = "d4a421d6eba4fbfa"
    optimize = "20170502"
    url = f"https://www.poderjudicial.es/search/AN/openDocument/{reference}/{optimize}"
    ref = parse_document_url(url)
    assert isinstance(ref, DocumentRef)
    assert ref.reference == reference
    assert ref.optimize == optimize
    assert reference in ref.access_to_pdf_url
    assert f"optimize={optimize}" in ref.access_to_pdf_url


def test_parse_document_url_preserves_hash_casing():
    reference = "D4A421D6EBA4FBFA"
    optimize = "20170502"
    url = f"https://www.poderjudicial.es/search/AN/openDocument/{reference}/{optimize}"
    ref = parse_document_url(url)
    assert ref.reference == reference


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/search/AN/openDocument/aabbccddeeff00112233445566778899/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/short/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/GGBBCCDDEeff00112233445566778899/20260911",
        "https://www.poderjudicial.es/search/AN/openDocument/aabbccddeeff00112233445566778899",
        "https://www.poderjudicial.es/search/AN/openDocument/aabbccddeeff00112233445566778899/202609115",
        "https://www.poderjudicial.es/search/AN/openDocument/d4a421d6eba4fbf/20170502",
        "https://www.poderjudicial.es/search/AN/openDocument/d4a421d6eba4fbfaaaaaaaaaaaaaaaa/20210705",
        "https://www.poderjudicial.es/search/AN/openDocument/d4a421d6eba4fbfaaaaaaaaaaaaaaaaaa/20210705",
        "https://www.poderjudicial.es/search/AN/openDocument//20170502",
        "https://www.poderjudicial.es/search/AN/openDocument/d4a421d6eba4fb-f/20170502",
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


def _make_document_ref(
    reference: str = REFERENCE,
    optimize: str = OPTIMIZE,
) -> DocumentRef:
    return DocumentRef(
        reference=reference,
        optimize=optimize,
        access_to_pdf_url="",
    )


# --- PDF destination resolver ---


def test_resolve_pdf_destination_prefers_env_var(monkeypatch, tmp_path):
    target = tmp_path / "from_env"
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(target))
    path, reason = resolve_pdf_destination()
    assert path == target
    assert reason == "NAVAJA_PDF_DIR"


def test_resolve_pdf_destination_uses_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_PDF_DIR", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    path, reason = resolve_pdf_destination()
    assert path == xdg / "navaja" / "pdfs"
    assert reason == "XDG_DATA_HOME"


def test_resolve_pdf_destination_uses_local_share_default(monkeypatch, tmp_path):
    monkeypatch.delenv("NAVAJA_PDF_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path, reason = resolve_pdf_destination()
    assert path == home / ".local" / "share" / "navaja" / "pdfs"
    assert reason == "XDG default"


# --- PDF writer ---


def test_save_pdf_creates_destination_directory(tmp_path):
    ref = _make_document_ref()
    dest = tmp_path / "new" / "pdfs"
    result = save_pdf(
        PDF_BYTES,
        'application/pdf; name="SAP_ML_110_2026.pdf"',
        ref,
        dest,
    )
    assert result.ok is True
    assert result.path == dest / "SAP_ML_110_2026.pdf"
    assert result.path.exists()
    assert result.path.read_bytes() == PDF_BYTES


def test_save_pdf_uses_server_sent_name(tmp_path):
    ref = _make_document_ref()
    result = save_pdf(
        PDF_BYTES,
        'application/pdf; name="SAP_ML_110_2026.pdf"',
        ref,
        tmp_path,
    )
    assert result.ok is True
    assert isinstance(result, PdfSaveResult)
    assert result.path == tmp_path / "SAP_ML_110_2026.pdf"
    assert result.reason == "server_sent_name"
    assert result.path.read_bytes() == PDF_BYTES


@pytest.mark.parametrize(
    "content_type",
    [
        'application/pdf; name="../../evil.pdf"',
        'application/pdf; name="/etc/passwd.pdf"',
        'application/pdf; name="~/.bashrc.pdf"',
        'application/pdf; name="..\\evil.pdf"',
        'application/pdf; name="folder/evil.pdf"',
        'application/pdf; name=".."',
        'application/pdf; name=""',
    ],
)
def test_save_pdf_rejects_unsafe_name(content_type, tmp_path):
    ref = _make_document_ref()
    result = save_pdf(PDF_BYTES, content_type, ref, tmp_path)
    assert result.ok is True
    assert result.path == tmp_path / f"{REFERENCE}_{OPTIMIZE}.pdf"
    # Nothing escaped the destination directory.
    assert all(p.parent == tmp_path for p in tmp_path.iterdir())


def test_save_pdf_rejects_absolute_path(tmp_path):
    ref = _make_document_ref()
    absolute = tmp_path / "outside.pdf"
    result = save_pdf(
        PDF_BYTES,
        f'application/pdf; name="{absolute}"',
        ref,
        tmp_path,
    )
    assert result.ok is True
    assert result.path == tmp_path / f"{REFERENCE}_{OPTIMIZE}.pdf"
    assert result.reason == "unsafe_name"


def test_save_pdf_fallback_when_header_missing(tmp_path):
    ref = _make_document_ref()
    result = save_pdf(PDF_BYTES, None, ref, tmp_path)
    assert result.ok is True
    assert result.path == tmp_path / f"{REFERENCE}_{OPTIMIZE}.pdf"
    assert result.reason == "missing_name"


def test_save_pdf_fallback_when_no_name_parameter(tmp_path):
    ref = _make_document_ref()
    result = save_pdf(PDF_BYTES, "application/pdf", ref, tmp_path)
    assert result.ok is True
    assert result.path == tmp_path / f"{REFERENCE}_{OPTIMIZE}.pdf"
    assert result.reason == "missing_name"


def test_save_pdf_appends_pdf_extension(tmp_path):
    ref = _make_document_ref()
    result = save_pdf(
        PDF_BYTES,
        'application/pdf; name="SAP_ML_110_2026"',
        ref,
        tmp_path,
    )
    assert result.ok is True
    assert result.path == tmp_path / "SAP_ML_110_2026.pdf"


def test_save_pdf_collision_identical_bytes(tmp_path):
    ref = _make_document_ref()
    content_type = 'application/pdf; name="SAP_ML_110_2026.pdf"'
    first = save_pdf(PDF_BYTES, content_type, ref, tmp_path)
    second = save_pdf(PDF_BYTES, content_type, ref, tmp_path)
    assert first.path == second.path
    assert second.reason == "identical_bytes"
    assert len(list(tmp_path.iterdir())) == 1


def test_save_pdf_collision_different_bytes(tmp_path):
    ref = _make_document_ref()
    content_type = 'application/pdf; name="SAP_ML_110_2026.pdf"'
    other_bytes = b"%PDF-1.4 different content"
    first = save_pdf(PDF_BYTES, content_type, ref, tmp_path)
    second = save_pdf(other_bytes, content_type, ref, tmp_path)
    assert first.path != second.path
    assert first.path.name == "SAP_ML_110_2026.pdf"
    assert second.path.name == "SAP_ML_110_2026_1.pdf"
    assert second.path.read_bytes() == other_bytes


def test_save_pdf_surfaces_mkdir_failure(monkeypatch, tmp_path):
    def _raise_permission_error(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("pathlib.Path.mkdir", _raise_permission_error)
    ref = _make_document_ref()
    result = save_pdf(PDF_BYTES, "application/pdf", ref, tmp_path)
    assert result.ok is False
    assert result.path is None
    assert result.reason == "mkdir_failed"
    assert "Permission denied" in result.error


def test_save_pdf_surfaces_write_failure(monkeypatch, tmp_path):
    def _raise_io_error(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("pathlib.Path.write_bytes", _raise_io_error)
    ref = _make_document_ref()
    result = save_pdf(PDF_BYTES, "application/pdf", ref, tmp_path)
    assert result.ok is False
    assert result.path is None
    assert result.reason == "write_failed"
    assert "No space left on device" in result.error


def test_save_pdf_skips_non_pdf_content_type(tmp_path):
    ref = _make_document_ref()
    result = save_pdf(HTML_DOC, "text/html", ref, tmp_path)
    assert result.ok is False
    assert result.path is None
    assert result.reason == "not_pdf"
    assert len(list(tmp_path.iterdir())) == 0


def test_save_pdf_surfaces_name_too_long(tmp_path):
    name_len = 300
    try:
        max_name = os.pathconf(tmp_path, "PC_NAME_MAX")
    except (ValueError, OSError):
        max_name = 255
    if max_name >= name_len:
        pytest.skip("filesystem supports names of this length")

    long_name = "a" * name_len
    ref = _make_document_ref()
    result = save_pdf(
        PDF_BYTES,
        f'application/pdf; name="{long_name}"',
        ref,
        tmp_path,
    )
    assert isinstance(result, PdfSaveResult)
    assert result.ok is True
    assert result.path == tmp_path / f"{REFERENCE}_{OPTIMIZE}.pdf"
    assert result.reason == "overlong_name"
    assert result.path.exists()
    assert result.path.read_bytes() == PDF_BYTES


def test_save_pdf_surfaces_directory_collision(tmp_path):
    ref = _make_document_ref()
    filename = "SAP_ML_110_2026.pdf"
    (tmp_path / filename).mkdir()
    result = save_pdf(
        PDF_BYTES,
        f'application/pdf; name="{filename}"',
        ref,
        tmp_path,
    )
    assert result.ok is False
    assert result.reason == "directory_collision"
    assert result.path is None
    assert result.error is not None


def test_save_pdf_surfaces_unreadable_collision(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")

    ref = _make_document_ref()
    filename = "existing.pdf"
    target = tmp_path / filename
    target.write_bytes(b"locked content")
    target.chmod(0o000)
    try:
        result = save_pdf(
            PDF_BYTES,
            f'application/pdf; name="{filename}"',
            ref,
            tmp_path,
        )
    finally:
        target.chmod(0o644)
    assert result.ok is False
    assert result.reason == "write_failed"
    assert result.path is None
    assert result.error is not None


# --- Full-text PDF save decision ---


def test_save_full_text_pdf_returns_not_attempted_when_fetch_failed():
    ref = _make_document_ref()
    result = FullTextResult(
        ok=False,
        content_type=None,
        text="",
        pdf_bytes=None,
        attempts=3,
        requests=4,
        error="captcha rejected",
    )
    save_result = save_full_text_pdf(result, ref)
    assert save_result == PdfSaveResult(
        ok=False,
        path=None,
        reason="not_attempted",
        error=None,
    )


def test_save_full_text_pdf_returns_not_pdf_when_no_pdf_bytes():
    ref = _make_document_ref()
    result = FullTextResult(
        ok=True,
        content_type="text/html",
        text="<p>html</p>",
        pdf_bytes=None,
        attempts=1,
        requests=2,
    )
    save_result = save_full_text_pdf(result, ref)
    assert save_result == PdfSaveResult(
        ok=False,
        path=None,
        reason="not_pdf",
        error=None,
    )


def test_save_full_text_pdf_returns_resolve_failed_when_destination_raises(
    monkeypatch,
):
    def _raise_runtime_error():
        raise RuntimeError("XDG variable points to a broken path")

    monkeypatch.setattr(
        "navaja.documents.resolve_pdf_destination",
        _raise_runtime_error,
    )
    ref = _make_document_ref()
    result = FullTextResult(
        ok=True,
        content_type="application/pdf",
        text="",
        pdf_bytes=PDF_BYTES,
        attempts=1,
        requests=2,
    )
    save_result = save_full_text_pdf(result, ref)
    assert save_result.ok is False
    assert save_result.path is None
    assert save_result.reason == "resolve_failed"
    assert "Could not resolve PDF destination" in save_result.error


def test_save_full_text_pdf_saves_pdf_and_returns_result(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVAJA_PDF_DIR", str(tmp_path))
    ref = _make_document_ref()
    result = FullTextResult(
        ok=True,
        content_type='application/pdf; name="SAP_ML_110_2026.pdf"',
        text="",
        pdf_bytes=PDF_BYTES,
        attempts=1,
        requests=2,
    )
    save_result = save_full_text_pdf(result, ref)
    assert save_result.ok is True
    assert save_result.path is not None
    assert save_result.path.parent == tmp_path
    assert save_result.path.name == "SAP_ML_110_2026.pdf"
    assert save_result.path.read_bytes() == PDF_BYTES
