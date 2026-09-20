"""navaja: MCP server for personal CENDOJ case-law research."""

from .captcha import (
    CaptchaAnswer,
    CaptchaTimeoutError,
    default_captcha_token_path,
    resolve_captcha_host,
    resolve_captcha_token,
    serve_captcha,
)
from .cendoj import (
    ALLOWED_RECORDS_PER_PAGE,
    CendojClient,
    Coleccion,
    Jurisdiccion,
    Localizacion,
    NivelLocalizacion,
    Orden,
    SearchError,
    SearchFilters,
    SearchGatedError,
    SearchRequestError,
    TipoResolucion,
    detect_refusal,
    parse_search_page,
)
from .documents import (
    DocumentRef,
    FullTextError,
    FullTextResult,
    parse_document_url,
)
from .models import MAX_RESULTS, SearchPage, Sentencia

__all__ = [
    "ALLOWED_RECORDS_PER_PAGE",
    "CaptchaAnswer",
    "CaptchaTimeoutError",
    "CendojClient",
    "Coleccion",
    "default_captcha_token_path",
    "detect_refusal",
    "DocumentRef",
    "FullTextError",
    "FullTextResult",
    "Jurisdiccion",
    "Localizacion",
    "MAX_RESULTS",
    "NivelLocalizacion",
    "Orden",
    "parse_document_url",
    "parse_search_page",
    "resolve_captcha_host",
    "resolve_captcha_token",
    "SearchError",
    "SearchFilters",
    "SearchGatedError",
    "SearchPage",
    "SearchRequestError",
    "Sentencia",
    "serve_captcha",
    "TipoResolucion",
]
