"""MCP server for personal CENDOJ case-law research.

The server exposes seven tools:

* ``buscar_sentencias`` — advanced search with filters, no captcha.
* ``listar_localizaciones`` — the site's own location vocabulary.
* ``ver_texto_completo`` — human-in-the-loop full-text fetch.
* ``iniciar_descargas`` — start a non-blocking batch of full-text fetches.
* ``estado_descargas`` — cheap metadata polling for the batch.
* ``recoger_descarga`` — collect one finished batch result.
* ``estado_servidor`` — runtime configuration snapshot.

It runs over the stdio transport, so stdout is reserved for JSON-RPC. Any
human-facing message (including the captcha form URL) is emitted on stderr.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import os
import sys
import threading
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer

from navaja import (
    CendojClient,
    FullTextError,
    Localizacion,
    NivelLocalizacion,
    SearchError,
    SearchFilters,
)
from navaja.cendoj import _coerce_enum
from navaja.captcha import (
    CaptchaBusyError,
    CaptchaTimeoutError,
    default_captcha_token_path,
    is_shared_captcha_server_running,
    resolve_captcha_host,
    resolve_captcha_token,
    start_shared_captcha_server,
    stop_shared_captcha_server,
)
from navaja.documents import (
    parse_document_url,
    save_full_text_pdf,
)
from navaja.jobs import JobQueue


_client_lock = threading.Lock()
_client: CendojClient | None = None


def _get_client() -> CendojClient:
    """Return the module-level client, creating it lazily under a lock."""
    global _client
    with _client_lock:
        if _client is None:
            _client = CendojClient()
        return _client


def close_shared_client() -> None:
    """Close the module-level client and release its connection pool."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


atexit.register(close_shared_client)


_queue_lock = threading.Lock()
_queue: JobQueue | None = None


def _max_concurrentes() -> int:
    """Return the configured worker count from ``NAVAJA_MAX_CONCURRENTES``."""
    raw = os.environ.get("NAVAJA_MAX_CONCURRENTES", "1").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"NAVAJA_MAX_CONCURRENTES must be an integer, got {raw!r}"
        ) from exc
    if value < 1:
        raise ValueError(f"NAVAJA_MAX_CONCURRENTES must be >= 1, got {value}")
    return value


def _get_queue() -> JobQueue:
    """Return the module-level job queue, creating it lazily under a lock."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = JobQueue(_run_fetch_job, max_concurrentes=_max_concurrentes())
        return _queue


def close_shared_queue() -> None:
    """Stop the module-level job queue and release its workers."""
    global _queue
    with _queue_lock:
        if _queue is not None:
            _queue.stop()
            _queue = None


atexit.register(close_shared_queue)


def _captcha_port() -> int:
    raw = os.environ.get("NAVAJA_CAPTCHA_PORT", "8765").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"NAVAJA_CAPTCHA_PORT must be an integer, got {raw!r}"
        ) from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"NAVAJA_CAPTCHA_PORT must be 1-65535, got {port}")
    return port


# Depth of nested `_captcha_lifespan` entries in this process. The listener is
# started on the outermost entry and stopped on the outermost exit only, so an
# inner exit can never tear down a listener an outer session still relies on.
_captcha_lifespan_lock = threading.Lock()
_captcha_lifespan_depth = 0


@contextlib.asynccontextmanager
async def _captcha_lifespan(app: MCPServer[Any]) -> AsyncIterator[None]:
    """Start the captcha listener with the session and stop it on exit.

    A failure to start (bad host, bad token, or an already-occupied port) is
    reported as a warning on stderr and does **not** crash the session. The
    tool path will raise the proper error when a captcha is actually needed.

    The listener is started only on the outermost entry and stopped only on the
    outermost exit, and the teardown runs from a ``finally`` so an exception
    raised inside the session body still releases the port.
    """
    global _captcha_lifespan_depth
    with _captcha_lifespan_lock:
        _captcha_lifespan_depth += 1
        outermost = _captcha_lifespan_depth == 1

    if outermost:
        try:
            host, host_reason = resolve_captcha_host()
            port = _captcha_port()
            token, token_reason = resolve_captcha_token()
            url = start_shared_captcha_server(host, port, token)
            print(
                f"Captcha form listening at {url} ({host_reason}, {token_reason})",
                file=sys.stderr,
                flush=True,
            )
        except (ValueError, RuntimeError) as exc:
            print(
                f"Captcha listener warning: could not start: {exc}",
                file=sys.stderr,
                flush=True,
            )

    try:
        yield
    finally:
        with _captcha_lifespan_lock:
            _captcha_lifespan_depth -= 1
            last = _captcha_lifespan_depth == 0
        if last:
            stop_shared_captcha_server()


server = MCPServer("navaja", version="0.0.1", lifespan=_captcha_lifespan)


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y")


def _parse_date_arg(value: str | None, name: str) -> date | None:
    """Convert ``YYYY-MM-DD`` or ``DD/MM/AAAA`` to a :class:`date`."""
    if value is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"{name} {value!r} is not a recognised date; use YYYY-MM-DD or DD/MM/AAAA"
    )


def _parse_localizaciones(items: list[str] | None) -> tuple[Localizacion, ...]:
    """Convert location strings to :class:`Localizacion` objects.

    Suffix forms such as ``"MELILLA(C)"`` are parsed exactly as the site
    uses them; bare names such as ``"Melilla"`` are treated as comunidades
    autónomas. Blank entries are ignored.
    """
    if not items:
        return ()
    result: list[Localizacion] = []
    for raw in items:
        text = raw.strip()
        if not text:
            continue
        try:
            loc = Localizacion.parse(text)
        except ValueError:
            loc = Localizacion(text)
        result.append(loc)
    return tuple(result)


@server.tool()
def buscar_sentencias(
    texto: str | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    jurisdiccion: str | None = None,
    tipo_resolucion: str | None = None,
    roj: str | None = None,
    ecli: str | None = None,
    num_resolucion: str | None = None,
    num_recurso: str | None = None,
    ponente: str | None = None,
    voces: str | None = None,
    localizacion: list[str] | None = None,
    coleccion: str | None = None,
    orden: str | None = None,
    campos_extra: dict[str, str] | None = None,
    pagina: int = 1,
    records_por_pagina: int = 10,
) -> dict:
    """Search the CENDOJ case-law database using filters.

    ``texto`` is optional, but at least one criterion is required. Filters
    combine with AND. Different ``localizacion`` entries are OR-ed with each
    other.

    How to answer a research question: to find the last N resolutions about a
    subject in a place, pass the place in ``localizacion`` and a subject wording
    in ``texto`` (for example ``"tráfico de drogas"``), add ``tipo_resolucion``
    when the question specifies sentences or orders, ask for a page of 10 to 50,
    and read the first N results.

    Classify results by ``materia``, not by the query wording. ``materia`` is
    the site's own subject label; free text is fuzzy, so a query for
    ``"tráfico de drogas"`` can also return drink-driving or extranjería
    resolutions that merely mention the phrase. The label is the reliable way
    to tell which results actually belong to the subject.

    ``total_capped`` is ``True`` when the reported total sits at the site's
    200-record ceiling, so the count is a ceiling rather than an exact count.
    Remedy: union several subject wordings, or narrow the search with a date
    range. There is no server-side subject filter; ``voces`` is honoured but
    does not include every resolution whose label says ``TRÁFICO DE DROGAS``,
    so it will miss relevant results.

    ``records_por_pagina`` accepts only 10, 20, 30 or 50, so asking for "the
    last 5" means requesting 10 and taking the first five. Any other value
    raises ``ValueError``.

    Returns structured metadata for each resolution: ROJ, ECLI, date, organ,
    seat, resolution and appeal numbers, municipality, ponente, the site's
    own automatic summary, document URL, and ``materia`` (the site's own
    subject label). The page itself carries ``total``, ``total_capped``,
    ``has_more``, ``page`` and ``records_per_page``.

    This tool does not solve any captcha; the search endpoint does not require
    one.

    Args:
        texto: free-text query, e.g. ``"clausulas abusivas"``.
        fecha_desde: inclusive lower bound, as ``YYYY-MM-DD`` or
            ``DD/MM/AAAA``.
        fecha_hasta: inclusive upper bound, as ``YYYY-MM-DD`` or
            ``DD/MM/AAAA``.
        jurisdiccion: one of ``CIVIL``, ``PENAL``, ``CONTENCIOSO``,
            ``SOCIAL``, ``MILITAR``.
        tipo_resolucion: ``SENTENCIA`` or ``AUTO``.
        roj: official citation token.
        ecli: European Case Law Identifier.
        num_resolucion: resolution number.
        num_recurso: appeal number.
        ponente: magistrate's name.
        voces: subject vocabulary, e.g. ``"TRÁFICO DE DROGAS"``.
        localizacion: list of location tokens. Each entry may be the site's
            suffix form (``"MELILLA(C)"``, ``"BARCELONA(P)"``,
            ``"MELILLA(S)"``) or a bare place name (``"Melilla"``), which
            means a comunidad autónoma. Use ``listar_localizaciones`` to get
            the valid names the site actually accepts.
        coleccion: ``AN`` for all jurisdictions or ``TS`` for the Tribunal
            Supremo only.
        orden: ``reciente`` (newest first) or ``antiguo`` (oldest first).
        campos_extra: escape hatch for site fields navaja does not model
            (``ID_NORMA``, ``SUBTIPORESOLUCION``, ...). Applied last, so it
            wins over a modelled field with the same name.
        pagina: real 1-based page number.
        records_por_pagina: one of 10, 20, 30 or 50. The site never returns
            more than 200 records for one query, so the requested window must
            not pass that ceiling.

    Raises:
        ValueError: when a date, the pagination, or the overall request is
            unusable (no criterion, unsupported page size, window past the
            200-record ceiling, or an enum token the site does not accept).
        SearchError: when the site refuses or does not honour the requested
            window. Subclasses :class:`SearchRequestError` (invalid search)
            and :class:`SearchGatedError` (mass-download control) may be
            raised instead.
    """
    filter_kwargs: dict[str, Any] = {
        "texto": texto,
        "fecha_desde": _parse_date_arg(fecha_desde, "fecha_desde"),
        "fecha_hasta": _parse_date_arg(fecha_hasta, "fecha_hasta"),
        "jurisdiccion": jurisdiccion,
        "tipo_resolucion": tipo_resolucion,
        "roj": roj,
        "ecli": ecli,
        "num_resolucion": num_resolucion,
        "num_recurso": num_recurso,
        "ponente": ponente,
        "voces": voces,
    }

    locations = _parse_localizaciones(localizacion)
    if locations:
        filter_kwargs["localizacion"] = locations

    if coleccion is not None:
        filter_kwargs["coleccion"] = coleccion
    if orden is not None:
        filter_kwargs["orden"] = orden

    filters = SearchFilters(**filter_kwargs)

    client = _get_client()
    page = client.search(
        filters,
        page=pagina,
        records_per_page=records_por_pagina,
        extra_fields=campos_extra,
    )
    return page.as_dict()


@server.tool()
def listar_localizaciones(
    nivel: str = "COMUNIDAD",
    comunidad: str | None = None,
    provincia: str | None = None,
) -> dict:
    """Return the site's own location vocabulary.

    The tokens are ready to pass into ``buscar_sentencias(localizacion=[...])``.
    A bare place name is also accepted by that parameter and means a comunidad
    autónoma; to use a provincia or sede, pass the suffix form returned here.

    ``nivel`` accepts ``COMUNIDAD`` (or ``C``), ``PROVINCIA`` (or ``P``) and
    ``SEDE`` (or ``S``), case-insensitively. ``PROVINCIA`` requires
    ``comunidad``; ``SEDE`` requires both ``comunidad`` and ``provincia``.

    Args:
        nivel: which level of the location hierarchy to list.
        comunidad: parent comunidad, required for ``PROVINCIA`` and ``SEDE``.
        provincia: parent provincia, required for ``SEDE``.

    Returns:
        A dict with ``nivel`` (the canonical level word) and
        ``localizaciones`` (the list of ready-to-use tokens).
    """
    member = _coerce_enum(nivel, NivelLocalizacion, "nivel")
    client = _get_client()
    tokens = client.localizaciones(
        nivel=member,
        comunidad=comunidad,
        provincia=provincia,
    )
    return {"nivel": member.name, "localizaciones": list(tokens)}


def _fetch_full_text_payload(url: str, *, espera_segundos: int) -> dict[str, Any]:
    """Fetch ``url`` and return the uniform payload used by every tool path.

    Both the blocking ``ver_texto_completo`` tool and the batch job runner
    call this helper so there is exactly one shape of call to
    :meth:`CendojClient.fetch_full_text` and one normalised payload shape.
    """
    try:
        ref = parse_document_url(url)
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "invalid_url",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
            "pdf_path": None,
            "pdf_save_reason": "not_attempted",
            "pdf_save_error": None,
        }

    host, host_reason = resolve_captcha_host()
    port = _captcha_port()
    token, _token_reason = resolve_captcha_token()

    print(
        f"Captcha form will bind to {host} because {host_reason}",
        file=sys.stderr,
        flush=True,
    )

    client = _get_client()
    captcha_url = f"http://{host}:{port}/{token}/"

    try:
        result = client.fetch_full_text(
            url,
            host=host,
            port=port,
            timeout=float(espera_segundos),
            token=token,
        )
    except CaptchaTimeoutError as exc:
        payload: dict[str, Any] = {
            "ok": False,
            "error": str(exc),
            "error_code": "captcha_timeout",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
            "pdf_path": None,
            "pdf_save_reason": "not_attempted",
            "pdf_save_error": None,
        }
        if hasattr(exc, "url"):
            payload["captcha_url"] = exc.url
        else:
            payload["captcha_url"] = captcha_url
        return payload
    except CaptchaBusyError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "captcha_busy",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
            "pdf_path": None,
            "pdf_save_reason": "not_attempted",
            "pdf_save_error": None,
            "captcha_url": captcha_url,
        }
    except FullTextError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "full_text_error",
            "attempts": None,
            "requests": None,
            "content_type": None,
            "text": None,
            "pdf_path": None,
            "pdf_save_reason": "not_attempted",
            "pdf_save_error": None,
        }

    pdf_save_result = save_full_text_pdf(result, ref)

    payload = {
        "ok": result.ok,
        "attempts": result.attempts,
        "requests": result.requests,
        "content_type": result.content_type,
        "text": result.text,
        "pdf_path": str(pdf_save_result.path) if pdf_save_result.path else None,
        "pdf_save_reason": pdf_save_result.reason,
        "pdf_save_error": pdf_save_result.error,
    }
    if result.error is not None:
        payload["error"] = result.error
        payload["error_code"] = "captcha_rejected"
    return payload


def _run_fetch_job(url: str) -> dict[str, Any]:
    """Batch job runner: fetch one URL and return a uniform payload.

    A queued job is not holding an MCP tool call open, so it uses the
    ``300`` second default already used by :func:`serve_captcha` rather than
    the ``120`` second default of the blocking ``ver_texto_completo`` tool.
    """
    return _fetch_full_text_payload(url, espera_segundos=300)


@server.tool()
def ver_texto_completo(
    url: str,
    espera_segundos: int = 120,
) -> dict:
    """Fetch the full text of a single CENDOJ resolution.

    This is a human-in-the-loop operation. If the site requests a captcha, the
    server opens a local browser form and blocks until a human types the answer.
    The exact URL to open is announced on stderr.

    Navaja deliberately contains no automatic captcha solver, OCR, vision model
    or third-party solving service.

    Args:
        url: a CENDOJ ``openDocument`` URL.
        espera_segundos: how long to wait for the human answer, in seconds.
            The default is ``120``, well below the MCP client request timeout
            configured for this deployment in ``~/.pi/agent/mcp.json``
            (``330`` seconds there), so the server has time to return a
            structured error before the client kills the call.

    Returns:
        A dict with ``ok``, ``attempts``, ``requests``, ``content_type`` and
        ``text``. When the final response is a PDF, the file is automatically
        saved to the directory resolved from ``NAVAJA_PDF_DIR`` (falling back
        to ``$XDG_DATA_HOME/navaja/pdfs`` and then
        ``~/.local/share/navaja/pdfs``). In that case the dict also carries
        ``pdf_path`` (the saved file path as a string), ``pdf_save_reason``
        (why the file has that name, e.g. ``server_sent_name`` or
        ``identical_bytes``), and ``pdf_save_error`` (``None`` on success).

        If the response is not a PDF, ``pdf_path`` is ``None`` and
        ``pdf_save_reason`` is ``not_pdf``. If saving fails, ``ok`` remains
        ``True`` (the fetch itself succeeded), ``text`` is unchanged, and
        ``pdf_save_error`` contains a human-readable failure message. A
        failure to resolve the destination directory itself is reported as
        ``pdf_save_reason="resolve_failed"``.

        On failure ``ok`` is ``False`` and the dict also carries ``error`` and
        ``error_code`` (``captcha_timeout``, ``captcha_busy``,
        ``captcha_rejected``, ``full_text_error`` or ``invalid_url``). The same
        PDF keys are always present so callers never have to probe for them,
        and on these error paths ``pdf_save_reason`` is ``not_attempted``
        because no response was fetched and no save was attempted. When a
        captcha timeout or ``captcha_busy`` occurs, ``captcha_url`` contains
        the real form URL so the caller can open it without reading stderr.
        The captcha answer itself is never returned.
    """
    return _fetch_full_text_payload(url, espera_segundos=espera_segundos)


def _captcha_form_url() -> str:
    """Return the real captcha form URL from the current environment."""
    host, _host_reason = resolve_captcha_host()
    port = _captcha_port()
    token, _token_reason = resolve_captcha_token()
    return f"http://{host}:{port}/{token}/"


def _normalise_runner_failure(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a generic queue runner crash into the uniform tool payload.

    The queue deliberately returns a minimal, runner-agnostic failure shape.
    This function adds the PDF keys and ``None`` placeholders that callers of
    ``ver_texto_completo`` already expect, so a collected job and a direct
    fetch remain interchangeable.
    """
    return {
        "ok": False,
        "error_code": "runner_failure",
        "error": payload.get("error"),
        "attempts": None,
        "requests": None,
        "content_type": None,
        "text": None,
        "pdf_path": None,
        "pdf_save_reason": "not_attempted",
        "pdf_save_error": None,
    }


@server.tool()
def iniciar_descargas(urls: list[str]) -> dict:
    """Start a non-blocking batch of full-text fetches.

    This is the first step of the batch flow. Call it with the list of
    CENDOJ ``openDocument`` URLs to fetch, then poll ``estado_descargas()``
    until the jobs reach a terminal state, and finally collect each result
    with ``recoger_descarga(job_id)``. The collected payload has exactly the
    same shape as a direct ``ver_texto_completo`` call.

    The captcha form URL is returned immediately, before any wait. A human
    can open it once and leave it open: the same solved challenge normally
    serves the whole batch because CENDOJ's captcha is session-sticky.

    The worker count comes from ``NAVAJA_MAX_CONCURRENTES`` and defaults to
    ``1``. That default is a product decision of this project, not a
    site-published limit: the repository contains no numeric quota, no
    ``Retry-After`` handling and no backoff code for CENDOJ.

    A job state of ``done`` means the runner returned; it does **not** mean
    the fetch succeeded. A document that failed at the fetch level (for
    example an unparsable URL) still ends in ``done`` with ``ok: false`` and
    an ``error_code``. Only an unexpected exception escaping the runner
    yields ``failed``. Callers must inspect ``ok`` and ``error_code`` per
    job and must not treat ``done`` as success.

    Args:
        urls: list of CENDOJ ``openDocument`` URLs.

    Returns:
        A dict with ``ok``, ``captcha_url``, ``max_concurrentes`` and
        ``jobs``. ``jobs`` is a list of ``{job_id, url, state}`` records in
        submission order. An empty ``urls`` list is refused with
        ``error_code="empty_urls"`` rather than silently accepted.
    """
    if not urls:
        return {
            "ok": False,
            "error_code": "empty_urls",
            "error": "urls must not be empty",
        }

    queue = _get_queue()
    job_ids = queue.submit_many(urls)
    meta_by_id = {meta["job_id"]: meta for meta in queue.estado()}
    captcha_url = _captcha_form_url()

    return {
        "ok": True,
        "captcha_url": captcha_url,
        "max_concurrentes": queue.max_concurrentes,
        "jobs": [
            {
                "job_id": job_id,
                "url": url,
                "state": meta_by_id[job_id]["state"],
            }
            for job_id, url in zip(job_ids, urls)
        ],
    }


@server.tool()
def estado_descargas() -> dict:
    """Poll the metadata of every batch job.

    This is the cheap polling step of the batch flow. It returns the state
    of every job started by ``iniciar_descargas`` without the full payload
    text, so polling a large batch is lightweight.

    The captcha form URL is available from ``iniciar_descargas`` before any
    wait; a human opens it once and leaves it open while the batch drains.

    The worker count defaults to ``1`` (from ``NAVAJA_MAX_CONCURRENTES``).
    That default is a product decision of this project, not a site-published
    limit.

    A job state of ``done`` means the runner returned; it does **not** mean
    the fetch succeeded. A document that failed at the fetch level still
    ends in ``done`` with ``ok: false`` and an ``error_code``. Only an
    unexpected exception escaping the runner yields ``failed``. Callers must
    inspect ``ok`` and ``error_code`` per job and must not treat ``done`` as
    success.

    Returns:
        A dict with ``ok`` and ``jobs``. ``jobs`` is a list of
        ``{job_id, url, state, attempts, pdf_path, error_code}`` metadata
        records in submission order. ``state`` is one of ``queued``,
        ``running``, ``done`` or ``failed``.
    """
    queue = _get_queue()
    return {"ok": True, "jobs": queue.estado()}


@server.tool()
def recoger_descarga(job_id: str) -> dict:
    """Collect the full payload for one finished batch job.

    This is the final step of the batch flow. When ``estado_descargas``
    reports a job as ``done`` (or ``failed``), call ``recoger_descarga``
    with its ``job_id`` to obtain the complete payload. A collected job has
    exactly the same shape as a direct ``ver_texto_completo`` response, so
    callers can treat the two paths identically.

    A state of ``done`` means the runner returned; it does **not** mean the
    fetch succeeded. A ``done`` payload may have ``ok: false`` and an
    ``error_code`` (for example ``invalid_url``). Callers must inspect
    ``ok`` and ``error_code`` and must not treat ``done`` as success. Only
    an unexpected exception escaping the runner yields ``failed``.

    While the job is still ``queued`` or ``running`` the result is a pending
    marker with ``error_code="pending"``. An unknown ``job_id`` returns
    ``error_code="unknown_job"``. If the runner itself crashed with an
    unexpected exception, the generic queue failure is normalised to the
    same uniform shape with ``error_code="runner_failure"``.

    The captcha form URL is available from ``iniciar_descargas`` before any
    wait; a human opens it once and leaves it open while the batch drains.

    Args:
        job_id: the identifier returned by ``iniciar_descargas``.

    Returns:
        The finished job payload in ``ver_texto_completo`` shape, a pending
        marker, or an unknown-job error.
    """
    queue = _get_queue()
    payload = queue.recoger(job_id)
    if isinstance(payload, dict) and payload.get("error_code") == "runner_failure":
        return _normalise_runner_failure(payload)
    return payload


@server.tool()
def estado_servidor() -> dict:
    """Return runtime server configuration.

    Returns the configured captcha host and port, whether a stable token has
    been set, whether the captcha listener is currently bound, and the real,
    usable captcha form URL.

    The form URL is part of the API surface: a caller can obtain it from this
    tool before running any blocking fetch, hand it to the human, and the human
    opens it once and leaves it open. While no challenge is pending the idle
    page refreshes itself every 5 seconds and turns into the captcha form
    automatically when a challenge arrives. The token in the URL gates access
    to the HTTP form, rather than being hidden from the caller; anyone with
    local access can already read it from the persisted token file.

    This call is not side-effect free: when no stable token is configured and
    no token file exists, resolving the token generates and persists one.
    Because of that, ``stable_token_set`` describes the configuration state as
    it was before this call.
    """
    host, _host_reason = resolve_captcha_host()
    port = _captcha_port()
    port_str = str(port)

    # Compute before resolving the token: resolution creates and persists a
    # token when none exists, which would make this flag vacuously true.
    env_token_set = bool(os.environ.get("NAVAJA_CAPTCHA_TOKEN", "").strip())
    stable_token_set = env_token_set
    if not stable_token_set:
        # A persisted token also gives a stable URL even without the env var.
        stable_token_set = default_captcha_token_path().exists()

    token, _token_reason = resolve_captcha_token()

    captcha_listening = is_shared_captcha_server_running(host, port)
    captcha_url = f"http://{host}:{port}/{token}/"

    return {
        "host": host,
        "port": port_str,
        "stable_token_set": stable_token_set,
        "captcha_listening": captcha_listening,
        "captcha_url": captcha_url,
    }


def main(argv: list[str] | None = None) -> None:
    """Run the navaja MCP server over stdio."""
    parser = argparse.ArgumentParser(
        prog="navaja-mcp",
        description="MCP server for personal CENDOJ case-law research",
    )
    parser.parse_args(argv)
    server.run(transport="stdio")
