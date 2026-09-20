"""Domain models for CENDOJ search results.

Every field here is exposed by the site's own results page, so none of it
requires solving the full-text captcha.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class Sentencia:
    """A single resolution offered by a CENDOJ search results page."""

    reference: str | None = None
    """Site-internal document id (``data-ref``)."""

    roj: str | None = None
    """Official citation, e.g. ``"SAP  NA 1461/2026"``."""

    ecli: str | None = None
    """European Case Law Identifier, e.g. ``"ECLI:ES:APNA:2026:1461"``."""

    tipo: str | None = None
    """Resolution type token, e.g. ``"SAP"``, ``"STS"``, ``"ATS"``."""

    sede: str | None = None
    """Court seat as printed in the result title, e.g. ``"Navarra"``."""

    fecha_resolucion: date | None = None
    """Date the resolution was issued."""

    num_resolucion: str | None = None
    municipio: str | None = None
    ponente: str | None = None
    num_recurso: str | None = None

    resumen: str | None = None
    """The site's own automatic summary, with markup stripped."""

    url_documento: str | None = None
    """Absolute URL of the document page for this resolution."""

    optimize: str | None = None
    """Index date token (``data-optimize``)."""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping, dates rendered as ISO strings."""
        data: dict[str, Any] = {}
        for field_name in self.__slots__:
            value = getattr(self, field_name)
            if isinstance(value, date):
                value = value.isoformat()
            data[field_name] = value
        return data


@dataclass(frozen=True, slots=True)
class SearchPage:
    """One page of CENDOJ search results."""

    sentencias: tuple[Sentencia, ...]
    page: int = 1
    records_per_page: int = 10
    total: int | None = None
    """Total hits reported by the site, when present."""

    @property
    def has_more(self) -> bool:
        """Whether the site reports more hits beyond this page."""
        if self.total is None:
            return len(self.sentencias) >= self.records_per_page
        return self.page * self.records_per_page < self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "records_per_page": self.records_per_page,
            "total": self.total,
            "has_more": self.has_more,
            "results": [s.as_dict() for s in self.sentencias],
        }
