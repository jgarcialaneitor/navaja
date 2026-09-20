"""HTTP client and parser for the CENDOJ case-law search.

Scope: search, metadata, automatic summaries, and human-in-the-loop full-text
retrieval. The full-text flow goes through the site's ``Control Descargas
masivas`` captcha; ``CendojClient.fetch_full_text`` opens a local browser form
for a human to solve it. See ``README.md`` and ``odd/tasks/cendoj-mcp.md``.

Endpoint map (established by read-only reconnaissance):

* ``GET  /search/indexAN.jsp``     bootstraps the ``JSESSIONID`` cookie.
* ``POST /search/search.action``   runs a query. No captcha.
* ``GET  /search/stickyImg``       the session-sticky captcha image.
* ``GET  /search/contenidos.action?action=accessToPDF`` requests a resolution.
* ``POST /search/contenidos.action?action=captcha`` submits the captcha answer.
"""

from __future__ import annotations

import re
from datetime import date
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
from .models import SearchPage, Sentencia

BASE_URL = "https://www.poderjudicial.es"
INDEX_URL = f"{BASE_URL}/search/indexAN.jsp"
SEARCH_URL = f"{BASE_URL}/search/search.action"

DEFAULT_TIMEOUT = 30.0
DEFAULT_SORT = "IN_FECHARESOLUCION:decreasing"
DEFAULT_RECORDS_PER_PAGE = 10

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


def parse_search_page(html: str, *, page: int = 1, base_url: str = BASE_URL) -> SearchPage:
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
        records_per_page=DEFAULT_RECORDS_PER_PAGE,
        total=_parse_total(soup),
    )


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
        response = self._client.get(INDEX_URL)
        response.raise_for_status()
        self._session_ready = True

    def search(
        self,
        texto: str,
        *,
        page: int = 1,
        records_per_page: int = DEFAULT_RECORDS_PER_PAGE,
        sort: str = DEFAULT_SORT,
        extra_fields: Mapping[str, str] | None = None,
    ) -> SearchPage:
        """Run a free-text query against the CENDOJ jurisprudence database.

        Args:
            texto: free-text query, e.g. ``"clausulas abusivas"``.
            page: 1-based page number.
            records_per_page: hits per page.
            sort: the site's sort token.
            extra_fields: additional form fields, for filters that are not
                modelled yet (jurisdiction, resolution type, date range).

        Returns:
            The parsed page of results.
        """
        if not texto or not texto.strip():
            raise ValueError("texto must be a non-empty search string")
        if page < 1:
            raise ValueError("page must be >= 1")

        self._ensure_session()

        data: dict[str, str] = {
            "action": "query",
            "sort": sort,
            "recordsPerPage": str(records_per_page),
            "databasematch": "AN",
            "TEXT": texto,
            "start": str(page),
        }
        if extra_fields:
            data.update(extra_fields)

        response = self._client.post(SEARCH_URL, data=data, headers={"Referer": INDEX_URL})
        response.raise_for_status()
        return parse_search_page(response.text, page=page)

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
            print(f"Fetching full text for {url}")
        else:
            print(prompt)

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
