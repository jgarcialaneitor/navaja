"""HTTP client and parser for the CENDOJ case-law search.

Scope: search, metadata, automatic summaries, and human-in-the-loop full-text
retrieval. The full-text flow goes through the site's ``Control Descargas
masivas`` captcha; ``CendojClient.fetch_full_text`` opens a local browser form
for a human to solve it. See ``README.md`` and ``odd/tasks/cendoj-mcp.md``.

Endpoint map (established by read-only reconnaissance):

* ``GET  /search/indexAN.jsp``     bootstraps the ``JSESSIONID`` cookie.
* ``POST /search/search.action``   runs a query. No captcha.
* ``POST /search/jurisprudencia.action`` serves the site's location vocabulary.
* ``GET  /search/stickyImg``       the session-sticky captcha image.
* ``GET  /search/contenidos.action?action=accessToPDF`` requests a resolution.
* ``POST /search/contenidos.action?action=captcha`` submits the captcha answer.
"""

from __future__ import annotations

import re
import sys
import threading
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Mapping

import httpx
from bs4 import BeautifulSoup, Tag

from .captcha import serve_captcha
from .documents import (
    CAPTCHA_IMAGE_URL,
    CONTENIDOS_URL,
    DocumentRef,
    FullTextResult,
    parse_document_url,
    _captcha_post_data,
    _extract_text,
    _is_captcha_page,
    _is_pdf,
)
from .models import MAX_RESULTS, SearchPage, Sentencia

BASE_URL = "https://www.poderjudicial.es"
INDEX_URL = f"{BASE_URL}/search/indexAN.jsp"
SEARCH_URL = f"{BASE_URL}/search/search.action"
LOCALIZACIONES_URL = f"{BASE_URL}/search/jurisprudencia.action"

DEFAULT_TIMEOUT = 30.0
DEFAULT_RECORDS_PER_PAGE = 10

# Page sizes the site accepts. Any other value comes back as its generic
# bad-request page, which parses as an empty result set.
ALLOWED_RECORDS_PER_PAGE = (10, 20, 30, 50)

_DATE_FORMAT = "%d/%m/%Y"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

_MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}

_TITLE_SPLIT = re.compile(r"\s*-\s*ROJ:\s*", re.IGNORECASE)
_SPANISH_DATE = re.compile(r"(\d{1,2})\s+de\s+([a-záéíóúü]+)\s+de\s+(\d{4})", re.IGNORECASE)
_COMPACT_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})\Z")
_RESUMEN_PREFIX = re.compile(r"^Resumen\s+Autom[aá]tico\s*:\s*", re.IGNORECASE)

# Matched against a normalised metadata label. Order matters: the first
# substring that hits wins.
_META_MATCHERS: tuple[tuple[str, str], ...] = (
    ("resoluci", "num_resolucion"),
    ("municipio", "municipio"),
    ("ponente", "ponente"),
    ("recurso", "num_recurso"),
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_label(label: str) -> str:
    label = label.lower().replace("º", "").replace("°", "")
    return re.sub(r"\s+", " ", label).strip().rstrip(":")


def parse_spanish_date(text: str) -> date | None:
    """Parse ``"11 de septiembre de 2026"`` or the compact ``"20260911"`` form."""
    if not text:
        return None
    match = _SPANISH_DATE.search(text)
    if match:
        day, month_name, year = match.groups()
        month = _MESES.get(month_name.lower())
        if month is not None:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
    compact = _COMPACT_DATE.match(text.strip())
    if compact:
        year, month, day = (int(part) for part in compact.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_title(text: str) -> tuple[str | None, str | None, date | None]:
    """Split ``"SAP Navarra, a 11 de septiembre de 2026 - ROJ: ..."``.

    Returns the resolution type token, the court seat, and the resolution date.
    """
    head = _TITLE_SPLIT.split(text, 1)[0]
    # Drop the temporal "a" ("SAP Navarra, a 11 de septiembre de 2026").
    head = re.sub(r",\s*a\s+", ", ", head)
    if "," in head:
        organo, fecha_text = head.split(",", 1)
    else:
        organo, fecha_text = head, ""
    organo = _clean(organo)
    tipo, _, sede = organo.partition(" ")
    return (tipo or None), (_clean(sede) or None), parse_spanish_date(fecha_text)


def _parse_metadatos(block: Tag) -> dict[str, Any]:
    """Extract ECLI and the labelled fields from a result's ``.metadatos`` list."""
    found: dict[str, Any] = {}
    for item in block.select(".metadatos li"):
        strong = item.find("b")
        value = _clean(strong.get_text(" ")) if strong else None
        if value and value.upper().startswith("ECLI"):
            found["ecli"] = value
            continue
        raw = _clean(item.get_text(" "))
        label = raw[: -len(value)] if value and raw.endswith(value) else raw
        label = _normalize_label(label)
        for needle, field_name in _META_MATCHERS:
            if needle in label:
                if field_name == "fecha_resolucion":
                    parsed = parse_spanish_date(value or "")
                    if parsed is not None:
                        found[field_name] = parsed
                elif value:
                    found[field_name] = value
                break
    return found


def _parse_total(soup: BeautifulSoup) -> int | None:
    """Read the hit count the site reports for the current query."""
    element = soup.select_one("input[name='total']")
    if element is not None:
        raw = element.get("value")
        if raw:
            try:
                return int(str(raw))
            except ValueError:
                return None
    return None


def parse_search_page(
    html: str,
    *,
    page: int = 1,
    records_per_page: int = DEFAULT_RECORDS_PER_PAGE,
    base_url: str = BASE_URL,
) -> SearchPage:
    """Parse a CENDOJ results page into a :class:`SearchPage`."""
    soup = BeautifulSoup(html, "lxml")
    sentencias: list[Sentencia] = []

    for block in soup.select("div.searchresult.doc"):
        anchor = block.select_one(".title a[data-roj]") or block.select_one(".title a[href]")
        if anchor is None:
            continue
        href = anchor.get("href")
        if not href or "openDocument" not in href:
            continue

        tipo, sede, fecha = _parse_title(_clean(anchor.get_text(" ")))
        meta = _parse_metadatos(block)

        summary_el = block.select_one(".summary")
        resumen = None
        if summary_el is not None:
            resumen = _RESUMEN_PREFIX.sub("", _clean(summary_el.get_text(" "))) or None

        url = str(href) if str(href).startswith("http") else f"{base_url}{href}"
        compact_date = parse_spanish_date(str(block.get("data-fechares") or ""))

        sentencias.append(
            Sentencia(
                reference=block.get("data-ref") or anchor.get("data-reference"),
                roj=anchor.get("data-roj"),
                ecli=meta.get("ecli"),
                tipo=tipo,
                sede=sede,
                fecha_resolucion=fecha or compact_date,
                num_resolucion=meta.get("num_resolucion"),
                municipio=meta.get("municipio"),
                ponente=meta.get("ponente"),
                num_recurso=meta.get("num_recurso"),
                resumen=resumen,
                url_documento=url,
                optimize=anchor.get("data-optimize"),
            )
        )

    return SearchPage(
        sentencias=tuple(sentencias),
        page=page,
        records_per_page=records_per_page,
        total=_parse_total(soup),
    )


class Jurisdiccion(StrEnum):
    """Tokens accepted by the site's ``JURISDICCION`` field.

    The site matches these exactly, in uppercase: ``"Penal"`` is answered with
    its generic bad-request page rather than being normalised.
    """

    CIVIL = "CIVIL"
    PENAL = "PENAL"
    CONTENCIOSO = "CONTENCIOSO"
    SOCIAL = "SOCIAL"
    MILITAR = "MILITAR"


class TipoResolucion(StrEnum):
    """Tokens accepted by the site's ``TIPORESOLUCION`` field."""

    SENTENCIA = "SENTENCIA"
    AUTO = "AUTO"


class Coleccion(StrEnum):
    """Which collection the site's ``databasematch`` field searches."""

    AN = "AN"
    """Every jurisdiction. The site's own default and navaja's."""

    TS = "TS"
    """Tribunal Supremo only."""


class Orden(StrEnum):
    """Sort tokens accepted by the site's ``sort`` field."""

    RECIENTE = "IN_FECHARESOLUCION:decreasing"
    ANTIGUO = "IN_FECHARESOLUCION:increasing"


class NivelLocalizacion(StrEnum):
    """Levels of the site's location selector, as its own suffixes."""

    COMUNIDAD = "C"
    PROVINCIA = "P"
    SEDE = "S"


_ENUMS: dict[str, type[StrEnum]] = {
    "jurisdiccion": Jurisdiccion,
    "tipo_resolucion": TipoResolucion,
    "coleccion": Coleccion,
    "orden": Orden,
}

_LOCALIZACION_TOKEN = re.compile(r"^(?P<nombre>.+?)\s*\((?P<nivel>[CPS])\)$")


def _coerce_enum(value: Any, enum_type: type[StrEnum], field_name: str) -> Any:
    """Coerce a token to ``enum_type``, refusing anything the site rejects.

    Both the member value (the token the site expects, such as
    ``"IN_FECHARESOLUCION:increasing"``) and the member name (``"antiguo"``)
    are accepted, case-insensitively. Names matter because several members are
    named for the caller's vocabulary while their value is the site's own
    token, and the tool exposes the friendly name.
    """
    if value is None or isinstance(value, enum_type):
        return value
    candidates = (value, str(value).strip().upper())
    for candidate in candidates:
        try:
            return enum_type(candidate)
        except ValueError:
            continue
    for candidate in candidates:
        member = enum_type.__members__.get(str(candidate).strip().upper())
        if member is not None:
            return member
    accepted = ", ".join(member.value for member in enum_type)
    names = ", ".join(
        member.name for member in enum_type if member.name != member.value
    )
    detail = f"{accepted} (or their names: {names})" if names else accepted
    raise ValueError(
        f"{field_name} {value!r} is not accepted by the site; use one of {detail}"
    )


@dataclass(frozen=True, slots=True)
class Localizacion:
    """One entry of the site's ``Localización`` filter.

    The site's wire format is its own display text -- ``MELILLA(C)`` for a
    comunidad autónoma, ``BARCELONA(P)`` for a provincia, ``MELILLA(S)`` for a
    sede -- and the names are its uppercase vocabulary. Names are uppercased
    here because the site matches them exactly.
    """

    nombre: str
    nivel: NivelLocalizacion = NivelLocalizacion.COMUNIDAD

    def __post_init__(self) -> None:
        nombre = re.sub(r"\s+", " ", str(self.nombre)).strip().upper()
        if not nombre:
            raise ValueError("localizacion name must not be empty")
        if any(char in nombre for char in "()|"):
            raise ValueError(
                f"localizacion name must be a bare place name, got {self.nombre!r}; "
                "use Localizacion(nombre, nivel) or the 'MELILLA(C)' string form"
            )
        object.__setattr__(self, "nombre", nombre)
        object.__setattr__(self, "nivel", _coerce_enum(self.nivel, NivelLocalizacion, "nivel"))

    @classmethod
    def parse(cls, token: str) -> "Localizacion":
        """Parse the site's own ``"MELILLA(C)"`` form."""
        match = _LOCALIZACION_TOKEN.match(str(token).strip())
        if match is None:
            raise ValueError(
                f"localizacion {token!r} must look like 'MELILLA(C)', 'BARCELONA(P)' "
                "or 'MELILLA(S)'"
            )
        return cls(match.group("nombre"), NivelLocalizacion(match.group("nivel")))

    def as_wire(self) -> str:
        """Return the site's display-text token, e.g. ``"MELILLA(C)"``."""
        return f"{self.nombre}({self.nivel.value})"


def _coerce_localizacion(value: Any) -> Localizacion:
    """Accept a :class:`Localizacion`, a ``"MELILLA(C)"`` string or a pair."""
    if isinstance(value, Localizacion):
        return value
    if isinstance(value, str):
        return Localizacion.parse(value)
    if isinstance(value, tuple) and len(value) == 2:
        return Localizacion(value[0], value[1])
    raise ValueError(
        "each localizacion must be a Localizacion, a 'MELILLA(C)' string or a "
        f"(name, level) pair, got {value!r}"
    )


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """The CENDOJ advanced-search filters modelled by navaja.

    Each field maps to one form field verified against the live endpoint. The
    fields deliberately left out (``ID_NORMA``, ``SUBTIPORESOLUCION``,
    ``TIPOORGANOPUB``, ...) stay reachable through
    :meth:`CendojClient.search`'s ``extra_fields``.
    """

    texto: str | None = None
    """Free text (``TEXT``). Optional when another criterion is present."""

    fecha_desde: date | None = None
    """Inclusive lower bound of ``FECHARESOLUCIONDESDE``."""

    fecha_hasta: date | None = None
    """Inclusive upper bound of ``FECHARESOLUCIONHASTA``."""

    jurisdiccion: Jurisdiccion | None = None
    tipo_resolucion: TipoResolucion | None = None
    roj: str | None = None
    ecli: str | None = None
    num_resolucion: str | None = None
    num_recurso: str | None = None

    ponente: str | None = None
    """Magistrate's name. Uppercased: the site's list is uppercase."""

    voces: str | None = None
    """Subject vocabulary (``VOCES``), e.g. ``"TRÁFICO DE DROGAS"``."""

    localizacion: tuple[Localizacion, ...] = ()
    """Comunidades autónomas, provincias or sedes. OR within this filter."""

    coleccion: Coleccion = Coleccion.AN
    """Which collection to search (``databasematch``)."""

    orden: Orden = Orden.RECIENTE
    """Result order (``sort``)."""

    def __post_init__(self) -> None:
        for name, enum_type in _ENUMS.items():
            object.__setattr__(self, name, _coerce_enum(getattr(self, name), enum_type, name))
        if self.texto is not None:
            object.__setattr__(self, "texto", re.sub(r"\s+", " ", str(self.texto)).strip() or None)
        for name in ("roj", "ecli", "num_resolucion", "num_recurso"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, str(value).strip() or None)
        for name in ("ponente", "voces"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, re.sub(r"\s+", " ", str(value)).strip().upper() or None)
        object.__setattr__(
            self,
            "localizacion",
            tuple(_coerce_localizacion(item) for item in (self.localizacion or ())),
        )
        if self.fecha_desde and self.fecha_hasta and self.fecha_desde > self.fecha_hasta:
            raise ValueError("fecha_desde must not be later than fecha_hasta")

    @property
    def has_criteria(self) -> bool:
        """Whether any criterion is set. The site needs at least one."""
        return any(
            (
                self.texto,
                self.fecha_desde,
                self.fecha_hasta,
                self.jurisdiccion,
                self.tipo_resolucion,
                self.roj,
                self.ecli,
                self.num_resolucion,
                self.num_recurso,
                self.ponente,
                self.voces,
                self.localizacion,
            )
        )

    def as_form_fields(self) -> dict[str, str]:
        """Map the filters to the site's own form field names and values."""
        fields: dict[str, str] = {
            "databasematch": self.coleccion.value,
            "sort": self.orden.value,
        }
        if self.texto:
            fields["TEXT"] = self.texto
        if self.fecha_desde:
            fields["FECHARESOLUCIONDESDE"] = self.fecha_desde.strftime(_DATE_FORMAT)
        if self.fecha_hasta:
            fields["FECHARESOLUCIONHASTA"] = self.fecha_hasta.strftime(_DATE_FORMAT)
        if self.jurisdiccion:
            fields["JURISDICCION"] = self.jurisdiccion.value
        if self.tipo_resolucion:
            fields["TIPORESOLUCION"] = self.tipo_resolucion.value
        if self.roj:
            fields["ROJ"] = self.roj
        if self.ecli:
            fields["ECLI"] = self.ecli
        if self.num_resolucion:
            fields["NUMERORESOLUCION"] = self.num_resolucion
        if self.num_recurso:
            fields["NUMERORECURSO"] = self.num_recurso
        if self.ponente:
            fields["PONENTE"] = self.ponente
        if self.voces:
            fields["VOCES"] = self.voces
        if self.localizacion:
            fields["VALUESCOMUNIDAD"] = "".join(
                f"{item.as_wire()} | " for item in self.localizacion
            )
        return fields


def _pagination_form_fields(page: int, records_per_page: int) -> dict[str, str]:
    """Validate pagination and map the page number to the site's record offset.

    The site's ``start`` is a 1-based record index, not a page number, so it
    must be derived rather than forwarded.
    """
    if page < 1:
        raise ValueError("page must be >= 1")
    if records_per_page not in ALLOWED_RECORDS_PER_PAGE:
        accepted = ", ".join(str(value) for value in ALLOWED_RECORDS_PER_PAGE)
        raise ValueError(
            f"records_per_page must be one of {accepted}; the site answers any other "
            "value with its generic bad-request page, which looks like an empty "
            "result set"
        )
    start = (page - 1) * records_per_page + 1
    last = start + records_per_page - 1
    if last > MAX_RESULTS:
        raise ValueError(
            f"page {page} at {records_per_page} per page reaches record {last}, past "
            f"the site's {MAX_RESULTS}-record ceiling; the site would silently return "
            f"all {MAX_RESULTS} records again instead of this page"
        )
    return {"recordsPerPage": str(records_per_page), "start": str(start)}


class SearchError(Exception):
    """Base class for a search the site answered without honouring the request.

    Covers three cases: the site refused the search as invalid, it served its
    mass-download control instead of results, and it answered the query but
    returned the whole result set rather than the requested page window.

    A search that legitimately matches nothing is *not* an error: it returns a
    result page with no results, which parses into an empty
    :class:`~navaja.models.SearchPage`. These exceptions mean the caller cannot
    trust the returned page as the requested slice, which must never look like
    "no matches".
    """


class SearchRequestError(SearchError):
    """The site rejected the search as invalid.

    Reachable with a field value the site validates strictly, such as an
    unaccepted page size or a jurisdiction token it does not know.
    """


class SearchGatedError(SearchError):
    """The site answered with its mass-download control instead of results.

    Its ``Control de grandes paginaciones`` challenge, the same control the
    full-text flow meets. navaja has no automatic solver; the request has to be
    made smaller or the challenge solved by a human elsewhere.
    """


# Matched case-insensitively against the response body. The mass-download gate
# is checked first: its page also carries the word "control".
_GATE_MARKERS = ("grandes paginaciones", "introduzca el texto que muestra")
_INVALID_SEARCH_MARKERS = ("no se ha podido atender", "la búsqueda no es válida")


def _error_message(html: str) -> str:
    """Return the site's own error text, when its error page is present."""
    soup = BeautifulSoup(html, "lxml")
    element = soup.select_one("span.errorMessage") or soup.select_one(".error")
    return _clean(element.get_text(" ")) if element is not None else ""


def detect_refusal(html: str) -> str | None:
    """Say why the site did not answer with results, when it did not.

    Returns ``"gated"`` for the mass-download control, ``"invalid"`` for the
    invalid-search page, and ``None`` for a result page -- including one with
    no results, which is an answer, not a refusal.
    """
    lowered = html.lower()
    if all(marker in lowered for marker in _GATE_MARKERS):
        return "gated"
    if any(marker in lowered for marker in _INVALID_SEARCH_MARKERS):
        return "invalid"
    return None


class CendojClient:
    """Synchronous client for the CENDOJ search endpoint.

    Use as a context manager to guarantee the connection pool is released::

        with CendojClient() as client:
            results = client.search("clausulas abusivas")
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        self._session_ready = False
        self._session_lock = threading.Lock()

    def __enter__(self) -> "CendojClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _ensure_session(self) -> None:
        """Fetch the search form once, so the ``JSESSIONID`` cookie is set."""
        if self._session_ready:
            return
        with self._session_lock:
            if self._session_ready:
                return
            response = self._client.get(INDEX_URL)
            response.raise_for_status()
            self._session_ready = True

    def search(
        self,
        filters: SearchFilters | str | None = None,
        *,
        page: int = 1,
        records_per_page: int = DEFAULT_RECORDS_PER_PAGE,
        extra_fields: Mapping[str, str] | None = None,
    ) -> SearchPage:
        """Run a query against the CENDOJ jurisprudence database.

        Args:
            filters: a :class:`SearchFilters`, or a plain string as shorthand
                for ``SearchFilters(texto=...)``.
            page: 1-based page number. It is mapped to the site's record offset
                rather than forwarded, because the site's ``start`` counts
                records.
            records_per_page: hits per page; one of
                :data:`ALLOWED_RECORDS_PER_PAGE`.
            extra_fields: additional form fields, for anything not modelled in
                :class:`SearchFilters` (``ID_NORMA``, ``SUBTIPORESOLUCION``,
                ...). Applied last, so they win over a modelled field with the
                same name.

        Returns:
            The parsed page of results.

        Raises:
            ValueError: when no criterion is given, or when the pagination
                cannot be expressed on the site.
            SearchRequestError: when the site rejects the search as invalid.
            SearchGatedError: when the site answers with its mass-download
                control instead of results.
        """
        if isinstance(filters, str):
            filters = SearchFilters(texto=filters)
        elif filters is None:
            filters = SearchFilters()

        if not filters.has_criteria:
            raise ValueError(
                "at least one search criterion is required: pass texto, a filter, or both"
            )

        data: dict[str, str] = {"action": "query"}
        data.update(_pagination_form_fields(page, records_per_page))
        data.update(filters.as_form_fields())
        if extra_fields:
            data.update(extra_fields)

        self._ensure_session()

        response = self._client.post(SEARCH_URL, data=data, headers={"Referer": INDEX_URL})
        response.raise_for_status()

        refusal = detect_refusal(response.text)
        if refusal == "gated":
            raise SearchGatedError(
                "the site answered with its mass-download control instead of results; "
                "navaja cannot solve that challenge automatically, so narrow the "
                "request"
            )
        if refusal == "invalid":
            message = _error_message(response.text)
            raise SearchRequestError(
                "the site rejected the search as invalid"
                + (f": {message}" if message else "")
            )

        page_result = parse_search_page(
            response.text, page=page, records_per_page=records_per_page
        )
        if page_result.clamped:
            raise SearchError(
                "the site returned the whole result set instead of the requested "
                f"page (got {len(page_result.sentencias)} records for a "
                f"{records_per_page}-record window)"
            )
        return page_result

    def localizaciones(
        self,
        nivel: NivelLocalizacion | str = NivelLocalizacion.COMUNIDAD,
        *,
        comunidad: str | None = None,
        provincia: str | None = None,
    ) -> tuple[str, ...]:
        """Return the site's own location vocabulary as ready-to-use tokens.

        The tokens include the level suffix, e.g. ``"MELILLA(C)"``, so they
        can be passed directly to :class:`SearchFilters` or
        ``buscar_sentencias``.

        Args:
            nivel: ``COMUNIDAD``, ``PROVINCIA`` or ``SEDE`` (or ``C``/``P``/``S``),
                case-insensitive.
            comunidad: required parent when ``nivel`` is ``PROVINCIA`` or
                ``SEDE``.
            provincia: required parent when ``nivel`` is ``SEDE``.

        Returns:
            A tuple of location tokens in the order the site sent them. The
            empty-key ``TODAS`` entry is dropped.

        Raises:
            ValueError: when a required parent is missing.
            SearchError: when the site's vocabulary payload cannot be read.
        """
        nivel_member = _coerce_enum(nivel, NivelLocalizacion, "nivel")
        if nivel_member is NivelLocalizacion.PROVINCIA and not comunidad:
            raise ValueError("comunidad is required for nivel=PROVINCIA")
        if nivel_member is NivelLocalizacion.SEDE:
            if not comunidad:
                raise ValueError("comunidad is required for nivel=SEDE")
            if not provincia:
                raise ValueError("provincia is required for nivel=SEDE")

        self._ensure_session()

        data = {
            "action": "getComunidades",
            "field": nivel_member.name,
            "comunidad": (comunidad or "").strip().upper(),
            "provincia": (provincia or "").strip().upper(),
            "publicinterface": "true",
        }
        response = self._client.post(
            LOCALIZACIONES_URL, data=data, headers={"Referer": INDEX_URL}
        )
        response.raise_for_status()

        try:
            payload = response.json()
        except Exception as exc:
            raise SearchError(
                "could not read the location vocabulary from the site: "
                "response was not valid JSON"
            ) from exc

        if not payload.get("success"):
            raise SearchError(
                "could not read the location vocabulary from the site: "
                "the site reported failure"
            )
        if "result" not in payload:
            raise SearchError(
                "could not read the location vocabulary from the site: "
                "missing result field"
            )

        result = payload["result"]
        if result is None:
            raise SearchError(
                "could not read the location vocabulary from the site: "
                "result field was null"
            )
        if not isinstance(result, str):
            raise SearchError(
                "could not read the location vocabulary from the site: "
                f"result field was {type(result).__name__!r}, expected a string"
            )
        if not result:
            return ()

        tokens: list[str] = []
        for entry in result.split("|"):
            if "&" not in entry:
                continue
            key, _, label = entry.partition("&")
            if not key:
                # Drop the site's empty-key "TODAS" option.
                continue
            tokens.append(f"{label}({nivel_member.value})")
        return tuple(tokens)

    def fetch_full_text(
        self,
        url: str,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        timeout: float = 300.0,
        token: str | None = None,
        prompt: str | None = None,
        max_captcha_attempts: int = 3,
    ) -> FullTextResult:
        """Fetch the full text of a CENDOJ resolution.

        If the site returns the captcha page, this opens a local browser form for a
        human to solve it. It does not implement any automatic solver.

        Args:
            url: a CENDOJ ``openDocument`` URL.
            host: interface for the local captcha form.
            port: port for the local captcha form; ``0`` picks a free port.
            timeout: seconds to wait for a human answer.
            token: optional URL-path token forwarded to the captcha form server.
                Must be URL-path-safe when supplied; see
                :func:`navaja.captcha.serve_captcha` for validation rules.
            prompt: optional message printed before blocking on the captcha.
            max_captcha_attempts: how many times to ask the human before giving up.

        Returns:
            A :class:`FullTextResult` describing the outcome.
        """
        self._ensure_session()
        ref = parse_document_url(url)

        if prompt is None:
            print(f"Fetching full text for {url}", file=sys.stderr, flush=True)
        else:
            print(prompt, file=sys.stderr, flush=True)

        response = self._client.get(
            ref.access_to_pdf_url,
            headers={"Referer": INDEX_URL},
        )
        response.raise_for_status()

        attempts = 0
        requests = 1
        if _is_captcha_page(response):
            for captcha_attempt in range(1, max_captcha_attempts + 1):
                attempts = captcha_attempt
                requests = captcha_attempt + 1
                image_response = self._client.get(
                    CAPTCHA_IMAGE_URL,
                    headers={"Referer": ref.access_to_pdf_url},
                )
                image_response.raise_for_status()

                answer = serve_captcha(
                    image_response.content,
                    host=host,
                    port=port,
                    timeout=timeout,
                    **({"token": token} if token is not None else {}),
                )
                post_data = _captcha_post_data(ref, str(answer))
                response = self._client.post(
                    CONTENIDOS_URL,
                    data=post_data,
                    headers={"Referer": ref.access_to_pdf_url},
                )
                response.raise_for_status()

                if not _is_captcha_page(response):
                    break

                if captcha_attempt == max_captcha_attempts:
                    content_type = response.headers.get("content-type")
                    return FullTextResult(
                        ok=False,
                        content_type=content_type,
                        text=_extract_text(content_type, response.content),
                        pdf_bytes=None,
                        attempts=attempts,
                        requests=requests,
                        error=(
                            "captcha answer was rejected; the site returned the "
                            "captcha page again after the maximum number of attempts"
                        ),
                    )

        content_type = response.headers.get("content-type")
        data = response.content
        pdf_bytes = data if _is_pdf(content_type, data) else None
        text = _extract_text(content_type, data)

        return FullTextResult(
            ok=True,
            content_type=content_type,
            text=text,
            pdf_bytes=pdf_bytes,
            attempts=attempts,
            requests=requests,
        )
