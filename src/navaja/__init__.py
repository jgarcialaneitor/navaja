"""navaja: MCP server for personal CENDOJ case-law research."""

from .captcha import CaptchaAnswer, CaptchaTimeoutError, serve_captcha
from .cendoj import CendojClient, parse_search_page
from .documents import (
    DocumentRef,
    FullTextError,
    FullTextResult,
    parse_document_url,
)
from .models import SearchPage, Sentencia

__all__ = [
    "CaptchaAnswer",
    "CaptchaTimeoutError",
    "CendojClient",
    "DocumentRef",
    "FullTextError",
    "FullTextResult",
    "SearchPage",
    "Sentencia",
    "parse_document_url",
    "parse_search_page",
    "serve_captcha",
]
