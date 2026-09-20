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
    def offset(self) -> int:
        """1-based index of this page's first record, as the site counts.

        The site's ``start`` parameter counts records, not pages, so page 2
        at 10 records per page begins at record 11.
        """
        return (self.page - 1) * self.records_per_page + 1

    @property
    def clamped(self) -> bool:
        """True when the site returned more records than this page asked for.

        Past its 200-record ceiling the site ignores ``recordsPerPage``,
        clamps ``start`` to ``201 - recordsPerPage``, and returns the whole
        result set in one page. That page looks legitimate but contains
        duplicates of the real window.
        """
        return len(self.sentencias) > self.records_per_page

    @property
    def has_more(self) -> bool:
        """Whether the site reports more hits beyond this page.

        The site counts records, not pages, so the decision is made from this
        page's first-record offset plus the records actually received: the last
        record held here is ``offset + len(sentencias) - 1``, and anything past
        it is more.

        Two traps, both measured live. Using the requested size instead of the
        records received is wrong for a short last page. And dropping the
        ``- 1`` hides a single remaining record: with a total of 11 hits and 10
        of them shown, this must say there is more, because the eleventh is
        reachable.
        """
        if self.total is None:
            return len(self.sentencias) >= self.records_per_page
        return self.offset + len(self.sentencias) - 1 < self.total

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "records_per_page": self.records_per_page,
            "total": self.total,
            "has_more": self.has_more,
            "results": [s.as_dict() for s in self.sentencias],
        }
