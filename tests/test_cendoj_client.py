"""Client tests.

The request-shape tests are offline: they assert what navaja *sends* using an
httpx mock transport. The live smoke test is opt-in via ``NAVAJA_LIVE=1`` so the
suite stays polite to a public service.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from navaja import CendojClient
from navaja.cendoj import (
    Coleccion,
    INDEX_URL,
    Jurisdiccion,
    LOCALIZACIONES_URL,
    Localizacion,
    NivelLocalizacion,
    Orden,
    SEARCH_URL,
    SearchError,
    SearchFilters,
    SearchGatedError,
    SearchRequestError,
    TipoResolucion,
    detect_refusal,
)
from navaja.documents import parse_document_url

FIXTURE = Path(__file__).parent / "fixtures" / "search_clausulas_abusivas.html"
HTML = FIXTURE.read_text(encoding="utf-8")

INVALID_FIXTURE = Path(__file__).parent / "fixtures" / "search_invalid_request.html"
GATE_FIXTURE = Path(__file__).parent / "fixtures" / "search_mass_download_gate.html"
NO_RESULTS_FIXTURE = Path(__file__).parent / "fixtures" / "search_no_results.html"
CLAMPED_FIXTURE = Path(__file__).parent / "fixtures" / "search_clamped_page.html"
TS_COLECCION_FIXTURE = Path(__file__).parent / "fixtures" / "search_ts_coleccion.html"
INVALID_HTML = INVALID_FIXTURE.read_text(encoding="utf-8")
GATE_HTML = GATE_FIXTURE.read_text(encoding="utf-8")
NO_RESULTS_HTML = NO_RESULTS_FIXTURE.read_text(encoding="utf-8")
CLAMPED_HTML = CLAMPED_FIXTURE.read_text(encoding="utf-8")
TS_COLECCION_HTML = TS_COLECCION_FIXTURE.read_text(encoding="utf-8")


def _client_capturing(sent: list[httpx.Request]) -> CendojClient:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        return httpx.Response(200, text=HTML)

    return CendojClient(transport=httpx.MockTransport(handler))


def _client_with_body(html: str) -> CendojClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        return httpx.Response(200, text=html)

    return CendojClient(transport=httpx.MockTransport(handler))


def _decoded_form(request: httpx.Request) -> dict[str, str]:
    parsed = parse_qs(request.content.decode())
    return {key: values[0] for key, values in parsed.items()}


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
    assert "sort=IN_FECHARESOLUCION%3Adecreasing" in body
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
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError):
            client.search(texto)
    assert sent == []


def test_rejects_non_positive_page():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError):
            client.search("x", page=0)
    assert sent == []


def test_sends_all_modelled_filters():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search(
            SearchFilters(
                texto="cláusulas abusivas",
                fecha_desde=date(2024, 1, 1),
                fecha_hasta=date(2024, 12, 31),
                jurisdiccion=Jurisdiccion.CIVIL,
                tipo_resolucion=TipoResolucion.SENTENCIA,
                roj="ROJ: STS 123/2024",
                ecli="ECLI:ES:TS:2024:123",
                num_resolucion="123/2024",
                num_recurso="1234/2024",
                ponente="Pérez",
                voces="tráfico",
                localizacion=(Localizacion("MELILLA"), Localizacion("Murcia")),
                coleccion=Coleccion.TS,
                orden=Orden.ANTIGUO,
            )
        )

    body = _decoded_form(sent[1])
    assert body == {
        "action": "query",
        "recordsPerPage": "10",
        "start": "1",
        "databasematch": "TS",
        "sort": "IN_FECHARESOLUCION:increasing",
        "TEXT": "cláusulas abusivas",
        "FECHARESOLUCIONDESDE": "01/01/2024",
        "FECHARESOLUCIONHASTA": "31/12/2024",
        "JURISDICCION": "CIVIL",
        "TIPORESOLUCION": "SENTENCIA",
        "ROJ": "ROJ: STS 123/2024",
        "ECLI": "ECLI:ES:TS:2024:123",
        "NUMERORESOLUCION": "123/2024",
        "NUMERORECURSO": "1234/2024",
        "PONENTE": "PÉREZ",
        "VOCES": "TRÁFICO",
        "VALUESCOMUNIDAD": "MELILLA(C) | MURCIA(C) | ",
    }


def test_filter_only_search_sends_no_text():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search(SearchFilters(localizacion=(Localizacion("MELILLA"),)))

    body = _decoded_form(sent[1])
    assert "TEXT" not in body
    assert body["VALUESCOMUNIDAD"] == "MELILLA(C) | "


def test_page_three_maps_to_start_twenty_one():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("x", page=3, records_per_page=10)

    assert _decoded_form(sent[1])["start"] == "21"


def test_page_twenty_maps_to_start_one_ninety_one():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("x", page=20, records_per_page=10)

    assert _decoded_form(sent[1])["start"] == "191"


@pytest.mark.parametrize("records_per_page", [0, 5, 25, 100])
def test_rejects_unsupported_records_per_page(records_per_page):
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError, match="records_per_page"):
            client.search("x", records_per_page=records_per_page)
    assert sent == []


def test_rejects_page_past_two_hundred_record_ceiling():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError, match="ceiling"):
            client.search("x", page=21, records_per_page=10)
    assert sent == []


def test_rejects_search_with_no_arguments():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError, match="criterion"):
            client.search()
    assert sent == []


def test_rejects_unknown_jurisdiccion_token():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError) as exc_info:
            client.search(SearchFilters(jurisdiccion="CONTENCIOSO-ADMINISTRATIVO"))
    assert "jurisdiccion" in str(exc_info.value)
    assert "CIVIL, PENAL, CONTENCIOSO, SOCIAL, MILITAR" in str(exc_info.value)
    assert sent == []


def test_mixed_case_tipo_resolucion_is_normalised():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search(SearchFilters(tipo_resolucion="sentencia"))

    assert _decoded_form(sent[1])["TIPORESOLUCION"] == "SENTENCIA"


def test_rejects_reversed_date_range():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        with pytest.raises(ValueError, match="fecha_desde"):
            client.search(
                SearchFilters(
                    fecha_desde=date(2024, 12, 31),
                    fecha_hasta=date(2024, 1, 1),
                )
            )
    assert sent == []


@pytest.mark.parametrize(
    "value, expected",
    [
        (Localizacion("Melilla"), "MELILLA(C)"),
        (Localizacion("melilla", NivelLocalizacion.SEDE), "MELILLA(S)"),
        ("Barcelona(P)", "BARCELONA(P)"),
        (("país vasco", "C"), "PAÍS VASCO(C)"),
    ],
)
def test_localizacion_grammar(value, expected):
    filters = SearchFilters(localizacion=(value,))
    assert filters.localizacion[0].as_wire() == expected


def test_multiple_localizaciones_are_joined_with_separator():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search(
            SearchFilters(
                localizacion=(
                    Localizacion.parse("MELILLA(C)"),
                    Localizacion.parse("MURCIA(C)"),
                )
            )
        )

    assert _decoded_form(sent[1])["VALUESCOMUNIDAD"] == "MELILLA(C) | MURCIA(C) | "


@pytest.mark.parametrize(
    "token",
    [
        "MELILLA",
        "MELILLA(X)",
        "MELILLA(C) | MURCIA(C)",
    ],
)
def test_rejects_malformed_localizacion_tokens(token):
    with pytest.raises(ValueError):
        SearchFilters(localizacion=(token,))


def test_localizacion_with_wire_token_is_rejected_as_bare_name():
    with pytest.raises(ValueError, match="bare place name"):
        Localizacion("MELILLA(C)")


def test_extra_fields_win_over_modelled_defaults():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search(SearchFilters(texto="x"), extra_fields={"databasematch": "TS"})

    assert _decoded_form(sent[1])["databasematch"] == "TS"


def test_non_default_records_per_page_reaches_wire():
    sent: list[httpx.Request] = []
    with _client_capturing(sent) as client:
        client.search("x", records_per_page=20)

    body = _decoded_form(sent[1])
    assert body["recordsPerPage"] == "20"
    assert body["start"] == "1"


@pytest.mark.parametrize(
    "html, expected",
    [
        (INVALID_HTML, "invalid"),
        (GATE_HTML, "gated"),
        (NO_RESULTS_HTML, None),
        (HTML, None),
    ],
)
def test_detect_refusal_classifies_response(html, expected):
    assert detect_refusal(html) == expected


def test_search_raises_search_request_error_with_invalid_fixture():
    with _client_with_body(INVALID_HTML) as client:
        with pytest.raises(SearchRequestError, match="La búsqueda no es válida") as exc_info:
            client.search("x")

    assert "invalid" in str(exc_info.value).lower()


def test_search_raises_search_gated_error_with_gate_fixture():
    with _client_with_body(GATE_HTML) as client:
        with pytest.raises(SearchGatedError):
            client.search("x")


def test_search_returns_empty_page_for_no_results_fixture():
    with _client_with_body(NO_RESULTS_HTML) as client:
        page = client.search("x")

    assert page.sentencias == ()
    assert page.total is None


def test_search_raises_search_error_when_site_returns_clamped_page():
    """The clamped fixture has 12 results; with the default 10-per-page window
    the parsed page is clamped and must raise SearchError.
    """
    with _client_with_body(CLAMPED_HTML) as client:
        with pytest.raises(SearchError, match="whole result set"):
            client.search("x", records_per_page=10)


def test_search_errors_are_search_error_subclasses():
    assert issubclass(SearchRequestError, SearchError)
    assert issubclass(SearchGatedError, SearchError)


def test_supreme_court_result_urls_are_accepted_by_the_url_validator():
    """Offline guard for the search-to-download contract.

    A Coleccion.TS search yields document URLs under ``/search/TS/``. The URL
    validator once accepted only ``/search/AN/``, so ``iniciar_descargas``
    refused every Supreme Court result. This runs on the default suite, from a
    captured real response, so the regression cannot come back unnoticed when
    the live test is deselected.
    """
    with _client_with_body(TS_COLECCION_HTML) as client:
        page = client.search(
            SearchFilters(texto="cláusula de conciencia", coleccion=Coleccion.TS),
            records_per_page=20,
        )

    urls = [s.url_documento for s in page.sentencias if s.url_documento]
    assert len(urls) == 20, "the captured Supreme Court response changed shape"
    assert all("/search/TS/" in url for url in urls), (
        "fixture no longer represents the Supreme Court path shape"
    )

    # Pin two known entries against literals, so a parser that returned
    # plausible-but-wrong values could not satisfy this test.
    first = parse_document_url(urls[0])
    assert first.reference == "ee62f935e8a3d299a0a8778d75e36f0d"
    assert first.optimize == "20260917"
    assert first.access_to_pdf_url == (
        "https://www.poderjudicial.es/search/contenidos.action"
        "?action=accessToPDF&publicinterface=true&tab=AN"
        "&reference=ee62f935e8a3d299a0a8778d75e36f0d"
        "&encode=true&optimize=20260917&databasematch=AN"
    )

    second = parse_document_url(urls[1])
    assert second.reference == "0064f3b31d94b1eea0a8778d75e36f0d"
    assert second.optimize == "20260917"

    # Every remaining URL must round-trip against its own path segments,
    # split independently of the validator's regex.
    for url in urls:
        ref = parse_document_url(url)
        _, collection, _, reference, optimize = urlparse(url).path.strip("/").split("/")
        assert collection == "TS"
        assert ref.reference == reference
        assert ref.optimize == optimize


def test_url_validator_error_message_names_every_accepted_collection():
    """The rejection message must describe what is actually accepted."""
    with pytest.raises(ValueError) as excinfo:
        parse_document_url(
            "https://www.poderjudicial.es/search/XX/openDocument/"
            "aabbccddeeff00112233445566778899/20260911"
        )

    message = str(excinfo.value)
    assert "AN" in message
    assert "TS" in message


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


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("NAVAJA_LIVE"),
    reason="live test: set NAVAJA_LIVE=1 to hit the real CENDOJ site",
)
def test_live_supreme_court_urls_are_parseable():
    """Regression: every Supreme Court URL the search yields must be accepted.

    The Coleccion.TS search returns document URLs under ``/search/TS/``. The
    URL validator used to accept only ``/search/AN/``, so ``iniciar_descargas``
    refused whole batches of Supreme Court results. The search endpoint needs
    no captcha, so this regression is checked against the real site; the
    captcha-gated fetch itself cannot be automated here.
    """
    with CendojClient() as client:
        page = client.search(
            SearchFilters(texto="cláusula de conciencia", coleccion=Coleccion.TS),
            records_per_page=20,
        )

    assert page.sentencias, "the live Supreme Court search returned no results"

    ts_urls = [s.url_documento for s in page.sentencias if s.url_documento]
    assert ts_urls, "no document URLs in the live Supreme Court results"
    assert any("/search/TS/" in url for url in ts_urls), (
        "expected at least one /search/TS/ URL; the site's path shape changed"
    )

    for url in ts_urls:
        ref = parse_document_url(url)
        _, collection, _, reference, optimize = urlparse(url).path.strip("/").split("/")
        assert collection in {"AN", "TS"}
        assert ref.reference == reference
        assert ref.optimize == optimize


# --- Location vocabulary tests ------------------------------------------------

COMUNIDAD_FIXTURE = Path(__file__).parent / "fixtures" / "localizaciones_comunidad.json"
PROVINCIA_MELILLA_FIXTURE = (
    Path(__file__).parent / "fixtures" / "localizaciones_provincia_melilla.json"
)
SEDE_MELILLA_FIXTURE = Path(__file__).parent / "fixtures" / "localizaciones_sede_melilla.json"

COMUNIDAD_JSON = COMUNIDAD_FIXTURE.read_text(encoding="utf-8")
PROVINCIA_MELILLA_JSON = PROVINCIA_MELILLA_FIXTURE.read_text(encoding="utf-8")
SEDE_MELILLA_JSON = SEDE_MELILLA_FIXTURE.read_text(encoding="utf-8")


def _client_for_localizaciones(json_body: str) -> CendojClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        if str(request.url) == LOCALIZACIONES_URL:
            return httpx.Response(200, text=json_body)
        return httpx.Response(404)

    return CendojClient(transport=httpx.MockTransport(handler))


def test_localizaciones_comunidad_parses_fixture():
    with _client_for_localizaciones(COMUNIDAD_JSON) as client:
        tokens = client.localizaciones()

    assert tokens == (
        "ANDALUCÍA(C)",
        "ARAGÓN(C)",
        "ASTURIAS(C)",
        "BALEARES(C)",
        "CANARIAS(C)",
        "CANTABRIA(C)",
        "CASTILLA LA MANCHA(C)",
        "CASTILLA Y LEÓN(C)",
        "CATALUÑA(C)",
        "CEUTA(C)",
        "COMUNIDAD VALENCIANA(C)",
        "EXTREMADURA(C)",
        "GALICIA(C)",
        "LA RIOJA(C)",
        "MADRID(C)",
        "MELILLA(C)",
        "MURCIA(C)",
        "NAVARRA(C)",
        "PAÍS VASCO(C)",
    )
    assert "TODAS" not in tokens


def test_localizaciones_provincia_melilla_parses_fixture():
    with _client_for_localizaciones(PROVINCIA_MELILLA_JSON) as client:
        tokens = client.localizaciones(NivelLocalizacion.PROVINCIA, comunidad="MELILLA")

    assert tokens == ("MELILLA(P)",)


def test_localizaciones_sede_melilla_parses_fixture():
    with _client_for_localizaciones(SEDE_MELILLA_JSON) as client:
        tokens = client.localizaciones(
            NivelLocalizacion.SEDE, comunidad="MELILLA", provincia="MELILLA"
        )

    assert tokens == ("MELILLA(S)",)


@pytest.mark.parametrize(
    "nivel, expected_field",
    [
        ("C", "COMUNIDAD"),
        ("comunidad", "COMUNIDAD"),
        (NivelLocalizacion.COMUNIDAD, "COMUNIDAD"),
        ("P", "PROVINCIA"),
        ("p", "PROVINCIA"),
        ("PROVINCIA", "PROVINCIA"),
        ("S", "SEDE"),
        ("sede", "SEDE"),
    ],
)
def test_localizaciones_level_aliases(nivel, expected_field):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        return httpx.Response(200, text=SEDE_MELILLA_JSON)

    with CendojClient(transport=httpx.MockTransport(handler)) as client:
        kwargs: dict[str, Any] = {"comunidad": "MELILLA", "provincia": "MELILLA"}
        if expected_field == "COMUNIDAD":
            kwargs = {}
        elif expected_field == "PROVINCIA":
            kwargs = {"comunidad": "MELILLA"}
        client.localizaciones(nivel, **kwargs)

    body = _decoded_form(sent[1])
    assert body["field"] == expected_field


def test_localizaciones_missing_comunidad_for_provincia_raises():
    with _client_for_localizaciones(PROVINCIA_MELILLA_JSON) as client:
        with pytest.raises(ValueError, match="comunidad"):
            client.localizaciones(NivelLocalizacion.PROVINCIA)


def test_localizaciones_missing_comunidad_for_sede_raises():
    with _client_for_localizaciones(SEDE_MELILLA_JSON) as client:
        with pytest.raises(ValueError, match="comunidad"):
            client.localizaciones(NivelLocalizacion.SEDE, provincia="MELILLA")


def test_localizaciones_missing_provincia_for_sede_raises():
    with _client_for_localizaciones(SEDE_MELILLA_JSON) as client:
        with pytest.raises(ValueError, match="provincia"):
            client.localizaciones(NivelLocalizacion.SEDE, comunidad="MELILLA")


def test_localizaciones_non_success_payload_raises_search_error():
    with _client_for_localizaciones('{"success":false,"errorCode":1,"result":""}') as client:
        with pytest.raises(SearchError, match="vocabulary"):
            client.localizaciones()


def test_localizaciones_non_json_body_raises_search_error():
    with _client_for_localizaciones("not json") as client:
        with pytest.raises(SearchError, match="vocabulary"):
            client.localizaciones()


def test_localizaciones_missing_result_raises_search_error():
    with _client_for_localizaciones('{"success":true,"errorCode":-1}') as client:
        with pytest.raises(SearchError, match="vocabulary"):
            client.localizaciones()


def test_localizaciones_empty_result_returns_empty_tuple():
    with _client_for_localizaciones('{"success":true,"errorCode":-1,"result":""}') as client:
        tokens = client.localizaciones()

    assert tokens == ()


@pytest.mark.parametrize(
    "result_value, kind",
    [
        ("null", "null"),
        ("123", "int"),
        ('["a"]', "list"),
    ],
)
def test_localizaciones_non_string_result_raises_search_error(result_value, kind):
    body = f'{{"success":true,"errorCode":-1,"result":{result_value}}}'
    with _client_for_localizaciones(body) as client:
        with pytest.raises(SearchError, match="vocabulary"):
            client.localizaciones()


def test_localizaciones_post_body_carries_expected_fields():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if str(request.url) == INDEX_URL:
            return httpx.Response(200, text="<html><body>form</body></html>")
        return httpx.Response(200, text=SEDE_MELILLA_JSON)

    with CendojClient(transport=httpx.MockTransport(handler)) as client:
        client.localizaciones(
            NivelLocalizacion.SEDE, comunidad="melilla", provincia="melilla"
        )

    assert sent[0].method == "GET"
    assert str(sent[0].url) == INDEX_URL
    assert sent[1].method == "POST"
    assert str(sent[1].url) == LOCALIZACIONES_URL
    body = _decoded_form(sent[1])
    assert body == {
        "action": "getComunidades",
        "field": "SEDE",
        "comunidad": "MELILLA",
        "provincia": "MELILLA",
        "publicinterface": "true",
    }
