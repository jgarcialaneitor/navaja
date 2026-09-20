"""Deterministic parser tests over a saved CENDOJ results page.

The fixture was captured once from the live site. Tests must never hit the
network: the site is a public service and the whole point of the fixture is to
keep the suite polite and reproducible.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from navaja import parse_search_page
from navaja.cendoj import parse_spanish_date

FIXTURE = Path(__file__).parent / "fixtures" / "search_clausulas_abusivas.html"


@pytest.fixture(scope="module")
def page():
    return parse_search_page(FIXTURE.read_text(encoding="utf-8"))


def test_parses_every_result_on_the_page(page):
    assert len(page.sentencias) == 10


def test_reports_total_hits(page):
    assert page.total == 200
    assert page.has_more is True


def test_first_result_is_fully_populated(page):
    first = page.sentencias[0]

    assert first.reference == "11851652"
    assert first.roj == "SAP  NA 1461/2026"
    assert first.ecli == "ECLI:ES:APNA:2026:1461"
    assert first.tipo == "SAP"
    assert first.sede == "Navarra"
    assert first.fecha_resolucion == date(2026, 9, 11)
    assert first.num_resolucion == "1097/2026"
    assert first.num_recurso == "1497/2024"
    assert first.municipio == "Pamplona/Iruña"
    assert first.ponente == "DANIEL RODRIGUEZ ANTUNEZ"


def test_automatic_summary_is_captured_and_unprefixed(page):
    first = page.sentencias[0]

    assert first.resumen is not None
    assert not first.resumen.startswith("Resumen")
    # The site highlights query terms with <font> markup; it must be stripped.
    assert "<font" not in first.resumen
    assert "cláusula" in first.resumen.lower()


def test_document_urls_are_absolute_and_point_at_openDocument(page):
    for sentencia in page.sentencias:
        assert sentencia.url_documento is not None
        assert sentencia.url_documento.startswith("https://www.poderjudicial.es/search/")
        assert "/openDocument/" in sentencia.url_documento


def test_every_result_has_a_roj_and_a_date(page):
    for sentencia in page.sentencias:
        assert sentencia.roj, f"missing ROJ: {sentencia}"
        assert sentencia.fecha_resolucion is not None, f"missing date: {sentencia}"


def test_as_dict_is_json_safe(page):
    payload = page.as_dict()

    assert payload["total"] == 200
    assert isinstance(payload["results"], list)
    assert payload["results"][0]["fecha_resolucion"] == "2026-09-11"
    assert isinstance(payload["results"][0]["resumen"], str)


class TestParseSpanishDate:
    def test_long_form(self):
        assert parse_spanish_date("11 de septiembre de 2026") == date(2026, 9, 11)

    def test_compact_form(self):
        assert parse_spanish_date("20260911") == date(2026, 9, 11)

    @pytest.mark.parametrize("value", ["", "no date here", "31 de febrero de 2026"])
    def test_unparseable_returns_none(self, value):
        assert parse_spanish_date(value) is None
