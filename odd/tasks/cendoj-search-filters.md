# Feature: cendoj-search-filters

Expose the CENDOJ advanced-search filters through `buscar_sentencias`, and stop
silently returning wrong or empty results when a request is malformed.

## Intent

`buscar_sentencias` today sends only `TEXT`. The site's own form carries ~30
fields, and the ones that matter for case-law research were verified live
(Engram: `navaja/cendoj-advanced-filters`, `navaja/cendoj-localizacion`).
Without them, geographic or subject scoping has to be smuggled into the free
text and then hand-filtered: a "tráfico de drogas en Melilla" query needed four
wording variants, 333 documents scanned and 31 hits filtered by hand.

Two defects also make the current tool lie: an unaccepted `records_por_pagina`
and a rejected filter value both come back as `total: null, results: []`, i.e.
indistinguishable from "no matches". A misspelled filter name is silently
ignored and returns unfiltered results.

## Scope

In scope:

- Typed, validated search filters on the client and on the MCP tool:
  free text, resolution-date range, jurisdiction, resolution type, ROJ, ECLI,
  resolution number, appeal number, ponente, `voces` (subject), collection
  (`AN`/`TS`), sort order, and location (comunidad autónoma / provincia / sede).
- Correct pagination: `pagina` becomes a page number mapped to the site's
  1-based record offset, and the request is refused when it would leave the
  site's absolute 200-record window.
- Distinguish a malformed request from a legitimate empty result set, and raise
  instead of returning `results: []`.
- Keep `extra_fields` as the escape hatch for the fields that are not modelled.

Out of scope, deliberately:

- `ID_NORMA`. Measured: a non-matching id returns a legitimate zero-hit
  page, so the field accepts an id space navaja does not know how to address.
  Out of scope until that id space is discoverable.
- `SUBTIPORESOLUCION`, `INSTITUCION`, `SECCION`, `SECCIONAUTO`,
  `SECCIONSOLOPLENO`, `TIPOORGANOPUB`, `TIPOINTERES_*`: present in the form,
  not verified, and not needed for the current research workflow.
- Raising the 200-record cap. `maxresults=500` and `maxresults=1000` are
  rejected by the site; the cap is absolute and no parameter lifts it.
- Validating location names against the site's vocabulary over the network
  (see Notes; the vocabulary endpoint is documented, not called).

## Constraints

- Deterministic tests never hit the live site; they assert the exact POST body
  through a fake transport, as `tests/test_cendoj_client.py` already does.
- No new dependency. The filters are plain dataclasses and enums.
- Wire values keep the site's names and grammar: the client is the only place
  that knows `TEXT`, `VALUESCOMUNIDAD`, `DD/MM/AAAA` or `IN_FECHARESOLUCION:*`.
- Never let a caller page past the window: refusing is correct, silently
  returning the first 200 records again is not.

## Frozen interface

Verified tokens, all measured against the live endpoint:

| Parameter | Wire field | Accepted values |
| --- | --- | --- |
| `texto` | `TEXT` | free text; optional if another criterion is present |
| `fecha_desde` / `fecha_hasta` | `FECHARESOLUCIONDESDE` / `FECHARESOLUCIONHASTA` | sent as `DD/MM/AAAA` |
| `jurisdiccion` | `JURISDICCION` | `CIVIL`, `PENAL`, `CONTENCIOSO`, `SOCIAL`, `MILITAR` |
| `tipo_resolucion` | `TIPORESOLUCION` | `SENTENCIA`, `AUTO` |
| `roj` / `ecli` | `ROJ` / `ECLI` | exact identifier |
| `num_resolucion` / `num_recurso` | `NUMERORESOLUCION` / `NUMERORECURSO` | as printed |
| `ponente` | `PONENTE` | full name, uppercase, as in the site's list |
| `voces` | `VOCES` | subject vocabulary, e.g. `TRÁFICO DE DROGAS` |
| `coleccion` | `databasematch` | `AN` (all, default), `TS` (Supreme Court only) |
| `orden` | `sort` | `reciente` (default), `antiguo` |
| `localizacion` | `VALUESCOMUNIDAD` | display-text grammar, see below |

Location grammar: `<NOMBRE>(C)` comunidad autónoma, `<NOMBRE>(P)` provincia,
`<NOMBRE>(S)` sede, several joined with ` | `. Names are the site's uppercase
vocabulary. Confirmed mappings: `MELILLA(C)` -> 50/50 results in Melilla,
`BARCELONA(P)` -> provincias of Barcelona, `MELILLA(C) | MURCIA(C)` -> exactly
those two. The field is OR within itself and AND with the other filters.

Pagination: `records_por_pagina` in `{10, 20, 30, 50}`; `pagina >= 1`;
`start = (pagina - 1) * records_por_pagina + 1`; and
`start + records_por_pagina - 1 <= 200`. Beyond the window the site clamps
`start` to `201 - records_por_pagina` and returns all 200 records in one page.

## Tasks

- [x] 1. `cendoj.py`: typed filters and payload construction. Add the frozen
      filter container and the location-grammar helper; build the POST payload
      from filters plus pagination; validate the page size against the site's
      accepted set, map `pagina` to `start`, refuse a window past 200, require
      at least one criterion, format dates as `DD/MM/AAAA` and validate the
      enum tokens. Tests assert the exact POST body per filter and every
      rejection path. Work unit `67c4885`.
- [x] 2. `cendoj.py` / `models.py`: separate "malformed request" from "no
      results". Characterise the response shapes first, record the
      discriminating signal in Evidence, and raise a typed error for a refusal
      instead of returning an empty page. It turned out to be three shapes, not
      two: the invalid-search page, the mass-download control, and a legitimate
      zero-hit page. Work unit `5dc7800`.
- [x] 3. `models.py`: fix the page metadata. `records_per_page` must reflect
      the request rather than the hardcoded constant, `has_more` must follow
      the offset semantics, and a response larger than the requested page
      (the clamped window) must not be reported as a normal page. Work unit
      `0dba1c8`.
- [ ] 4. `server.py`: expose the filters on `buscar_sentencias` with typed
      parameters, ISO date strings in, enums for the closed vocabularies,
      `texto` optional under the at-least-one-criterion rule, and
      `extra_fields` documented as the escape hatch. The tool docstring states
      each accepted value set and the 200-record ceiling.
- [ ] 5. Documentation: README filter table, the location grammar and the
      vocabulary endpoint, what remains unsupported, and the evidence for each
      claim. Close this file with the verification record.

## Evidence

Measured live against `https://www.poderjudicial.es/search/search.action` over
`databasematch=AN`, save for the two sub-second confirmations noted below.

Field verification, and the reason the earlier negative result was wrong:

```
IGNORADO  (control: campo inexistente)  len=48326 first3=[...]   identical to baseline
IGNORADO  VALUESCOMUNIDAD=ALL@ALL@MELILLA                        the internal codes
HONRADO   VALUESCOMUNIDAD=MELILLA(C) |  total=200 -> municipio=Melilla only
```

Filter effect, with values chosen to exclude the baseline's first results:

```
HASTA=31/12/2025        -> first result 2025-12-30 (2026 excluded)
DESDE..HASTA 2020       -> first result 2020-12-30
PONENTE=SANTOS PEÑALVER -> SAP ML 163/2026, SAP ML 155/2026, same ponente
databasematch=TS        -> only ATS/STS
sort=...:increasing     -> oldest first
CCAA sweep (MELILLA(C)) -> 50/50 municipios == ['Melilla']
```

Pagination window, `pos[n]` being the n-th record of the 200-record result set:

```
rpp=10 start=191 n=10  first == pos[191]  in window
rpp=10 start=192 n=200 first == pos[191]  clamped, whole set returned
rpp=50 start=151 n=50  first == pos[151]  in window
rpp=50 start=152 n=200 first == pos[151]  clamped, whole set returned
maxresults=500 / 1000 -> rejected; the 200 cap cannot be lifted
```

Caveat recorded for task 2: an earlier probe of `start=201` reported the first
page because the payload carried `start` twice and the server honoured the
first value. Do not repeat that; build the payload with a single `start`.

### Task 3 verification (work unit `0dba1c8`)

- Test counts: 157 passed + 1 skipped before the work unit, 177 passed + 1
  skipped after.
- The clamp was measured to be narrower than the window rule suggests: with a
  small result set (`total=1`, `total=4`), requesting `start=11` through
  `start=201` returns an empty page, not a clamp and not a reset. Only a set
  that reaches the 200 ceiling clamps. The refusal in `search()` is therefore
  defensive, and an empty page past the end of a short result set stays a
  normal empty page.
- Two regression gaps were found during verification and closed with tests
  before the commit: no existing `has_more` case told the offset arithmetic
  apart from the old `page * records_per_page < total`, and nothing drove
  `CendojClient.search` into a clamped response, so deleting the refusal would
  have left every test green.
- `as_dict()` keys and the dataclass field order are unchanged, so the MCP tool
  response shape does not move in this work unit.

### Task 2 verification (work unit `5dc7800`)

Three shapes, told apart by content and not by size:

| case | body text | outcome |
| --- | --- | --- |
| invalid search | `No se ha podido atender su petición.` / `La búsqueda no es válida!` | `SearchRequestError` |
| mass-download gate | `Control de grandes paginaciones` | `SearchGatedError` |
| no results | `No se ha encontrado ningún resultado.` | empty `SearchPage` |

Size is not a signal: a legitimate zero-hit page and an invalid-search page can
be the same size, and the shapes measured at 275, 398, 554, 744, 954, 1922 and
1999 bytes inside the same family. An earlier draft of this task read "empty
275-byte page = bad request", which was wrong twice over: the size is not
guaranteed, and `ID_NORMA=6311` -- assumed rejected -- is in fact a legitimate
zero-hit page.

New finding: `maxresults` above the 200 cap does not merely fail. It makes the
site answer with its `Control de grandes paginaciones` challenge, the same
captcha the full-text flow meets. The modelled filters never send `maxresults`,
but `extra_fields` can, which is why the gate is surfaced as its own error.

Test counts: 149 passed + 1 skipped before the work unit, 157 passed + 1 skipped
after, with no test removed (five test functions added; the extra cases come
from a parametrised one). The delegated writer reported 119 passing for the
full suite, which was simply wrong; the parent re-measured at HEAD and in the
worktree rather than trusting it.

### Task 1 verification (work unit `67c4885`)

- `tests/test_cendoj_client.py`: 31 passed, 1 skipped (the skip is the opt-in
  live smoke test). Full suite: 149 passed, 1 skipped.
- The wire mapping is asserted as exact dictionary equality of the decoded form
  body, so a missing, extra or misspelled field name fails rather than passing
  quietly, and `PONENTE`/`VOCES` uppercasing is pinned.
- Regression traps are covered by a test each: `page=3` sends `start=21` and
  `page=20` sends `start=191` (so reverting `start` to the page number fails),
  `records_per_page` in `{0, 5, 25, 100}` is refused with no request sent, and
  a non-default `records_per_page` reaches the wire.
- Independent verification raised three hardening gaps, all closed before the
  commit: `test_rejects_empty_query` and `test_rejects_non_positive_page` now
  assert that no request was sent, the default `sort` token is asserted, and
  `recordsPerPage` is asserted for a value other than the default.

## Notes

- The location vocabulary is discoverable without hardcoding names:
  `POST /search/jurisprudencia.action` with
  `action=getComunidades&field=COMUNIDAD|PROVINCIA|SEDE&comunidad=&provincia=&publicinterface=true`
  returns pipe-separated `KEY&LABEL` pairs (`MELILLA&MELILLA`,
  `PAÍS VASCO&PAÍS VASCO`). Left out of scope to keep the tool offline-testable;
  it is the natural follow-up if name validation is ever wanted.
- A filter-only search works: `TEXT` may be empty as long as another criterion
  is present. `TEXT=""` with no criteria is a bad request.
- `ccaa` is not the location field; it was probed and returned mixed
  municipalities.
