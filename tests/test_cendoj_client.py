"""Client tests.

The request-shape tests are offline: they assert what navaja *sends* using an
httpx mock transport. The live smoke test is opt-in via ``NAVAJA_LIVE=1`` so the
suite stays polite to a public service.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from navaja import CendojClient
from navaja.cendoj import INDEX_URL, SEARCH_URL

FIXTURE = Path(__file__).parent / "fixtures" / "search_clausulas_abusivas.html"
HTML = FIXTURE.read_text(encoding="utf-8")


def _client_capturing(sent: list[httpx.Request]) -> CendojClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        return httpx.Response(200, text=HTML)

    return CendojClient(transport=httpx.MockTransport(handler))


def test_bootstraps_session_before_searching():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("clausulas abusivas")

    assert sent[0].method == "GET"
    assert str(sent[0].url) == INDEX_URL
    assert sent[1].method == "POST"
    assert str(sent[1].url) == SEARCH_URL


def test_sends_the_expected_form_fields():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("clausulas abusivas")

    body = sent[1].content.decode()
    assert "action=query" in body
    assert "databasematch=AN" in body
    assert "TEXT=clausulas+abusivas" in body


def test_session_is_reused_across_searches():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("uno")
        client.search("dos")

    gets = [r for r in sent if r.method == "GET"]
    posts = [r for r in sent if r.method == "POST"]
    assert len(gets) == 1, "the index page must only be fetched once"
    assert len(posts) == 2


def test_extra_fields_are_forwarded():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("x", extra_fields={"JURISDICCION": "CIVIL"})

    assert "JURISDICCION=CIVIL" in sent[1].content.decode()


@pytest.mark.parametrize("texto", ["", "   "])
def test_rejects_empty_query(texto):
    with _client_capturing([]) as client:
        with pytest.raises(ValueError):
            client.search(texto)


def test_rejects_non_positive_page():
    with _client_capturing([]) as client:
        with pytest.raises(ValueError):
            client.search("x", page=0)


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("NAVAJA_LIVE"),
    reason="live test: set NAVAJA_LIVE=1 to hit the real CENDOJ site",
)
def test_live_search_smoke():
    with CendojClient() as client:
        page = client.search("clausulas abusivas", records_per_page=10)

    assert page.sentencias, "the live search returned no results"
    assert page.sentencias[0].roj
    assert page.sentencias[0].url_documento
