# Feature: cendoj-search-usability

Make the search tool answer a research question in fewer attempts, by exposing
the site's own subject classification and by telling the caller what the
endpoint cannot do.

## Intent

A full test run ("the last 5 sentences about drug trafficking in Melilla") took
about eight tool calls and could still have been wrong: two of the five correct
resolutions are invisible to the filter that looks right. The cause is not the
filter set. The site's subject axis is not filterable, the 200-record ceiling is
invisible, and nothing in the tool says either thing, so every caller
rediscovers it by trial and error.

Measured facts behind this feature live in Engram under
`navaja/cendoj-recall-strategy` and `navaja/cendoj-subject-filtering`.

## Scope

In scope:

- A first-class `materia` field per result, taken from the site's own label, so
  callers stop parsing it out of the summary string.
- An honest signal when the reported total is the site's ceiling.
- The answering recipe inside the tool's own description, so the model does not
  have to discover the endpoint's behaviour by probing.
- A `listar_localizaciones` tool backed by the site's own vocabulary endpoint,
  so place tokens stop being guessed.

Out of scope, deliberately, because each was measured and does not work:

- Server-side subject filtering. `MATERIAS` is a real form field but the
  endpoint ignores it (three values, including a valid one, returned the
  baseline byte-identically). Free-text operators do nothing: `"tráfico de
  drogas"` in quotes equals the unquoted query, `+tráfico +drogas` is worse
  (it pulls in a drink-driving resolution), and `AND` / `Y` change nothing.
  `VOCES` is honoured but is a different thesaurus: it misses `SAP ML 53/2026`
  and `SAP ML 38/2026`, which carry the `TRÁFICO DE DROGAS` label.
- A tool that unions query wordings internally. It would multiply requests to a
  public service and hide the method.
- The `materiasleg` vocabulary (6481 entries, published as a static array in
  `/search/js/autocompleterLocal_materiasleg.js`). It is the legislation axis
  and does not contain `TRÁFICO DE DROGAS`.

## Constraints

- Deterministic tests run against saved fixtures; each new fixture costs exactly
  one live capture.
- `resumen` keeps its current bytes. Adding `materia` must not change any
  existing field.
- The 200-record ceiling moves to `models.py`, where the domain fact belongs,
  and stays importable from `navaja`.

## Frozen interface

`Sentencia.materia: str | None`

- Source: the `.summary` text. When it matches `^RESUMEN:\s*(.+)$`
  case-insensitively, the captured group is the label the site assigned.
- `None` when the summary carries no label form, or when the label is one of the
  two unclassified markers: `DELITO SIN ESPECIFICAR`, `MATERIAS NO ESPECIFICADAS`.
- `resumen` is unchanged, so the label still appears there for a caller that
  wants the raw text.

`SearchPage.total_capped: bool`

- `total is not None and total >= MAX_RESULTS`.
- Documented as a heuristic: a query with exactly 200 real hits reads the same
  as a capped one. The alternative -- a caller concluding "there are 200" when
  the site truncated -- is worse.

`listar_localizaciones(nivel="COMUNIDAD", comunidad=None, provincia=None)`

- `nivel` accepts `COMUNIDAD`, `PROVINCIA`, `SEDE` and the short forms
  `C`, `P`, `S`.
- Returns `{"nivel": <canonical>, "localizaciones": [<label>, ...]}`.
- The label is exactly what `buscar_sentencias(localizacion=[...])` accepts.
- The endpoint's empty-key entry (the site's own "TODAS", meaning no filter) is
  dropped.
- Drilling down needs the parent: `PROVINCIA` needs `comunidad`, `SEDE` needs
  both `comunidad` and `provincia`. A missing parent raises `ValueError`.

Wire contract for the vocabulary endpoint, verified live:

```
POST /search/jurisprudencia.action
  action=getComunidades&field=COMUNIDAD|PROVINCIA|SEDE
  &comunidad=<parent>&provincia=<parent>&publicinterface=true
-> {"success": true, "result": "&TODAS|ANDALUCÍA&ANDALUCÍA|...|MELILLA&MELILLA"}
```

Each `KEY&LABEL` pair is pipe-separated; the label is the usable value.

## Tasks

- [x] 1. `models.py` / `cendoj.py` / `__init__.py`: the data. Add `materia` to
      `Sentencia` (parsed from the summary label, with both unclassified markers
      normalised to `None`), add `total_capped` to `SearchPage`, and move
      `MAX_RESULTS` into `models.py` with `cendoj.py` importing it from there and
      `navaja` still exporting it. Tests cover the label forms, both unclassified
      markers, a label-free summary, `total_capped` true / false / unknown, and
      that `resumen` did not change. Work unit `0cb9407`.
- [x] 2. `server.py`: the guidance. Put the answering recipe in the tool
      description: how to answer "the last N about X in Y" (place + a subject
      wording + a page of 10 to 50 + classify by `materia`), that no
      server-side subject filter exists and why `voces` is not a substitute, the
      200-record ceiling and what to do when `total_capped` is true, and the
      accepted page sizes. Tests assert the description carries those points and
      that `materia` and `total_capped` reach the tool's response. Work unit
      `7e416e9`.
- [x] 3. The vocabulary tool. Add `CendojClient.localizaciones(...)` for the
      endpoint above, the `listar_localizaciones` MCP tool on top of it, and one
      saved fixture per level captured live. Tests cover the parsing (including
      the empty-key entry and the `&` escape), the parent requirements, the
      level aliases, and the tool's response shape. Work unit `a46c368`.
- [x] 4. Documentation: README section for `materia`, `total_capped` and
      `listar_localizaciones`, with the recipe and the measured reasons the
      server-side alternatives do not work. Close this file with the
      verification record. Work unit: the closing commit that carries this
      section.

## Evidence

Every claim above was measured against the live endpoint.

Where the subject actually lives:

```
RESUMEN: TRÁFICO DE DROGAS GRAVE DAÑO A LA SALUD   <- the site's label
RESUMEN: DELITO SIN ESPECIFICAR                    <- unclassified marker
RESUMEN: MATERIAS NO ESPECIFICADAS                 <- unclassified marker
<free sentence>                                    <- no label at all
```

`_RESUMEN_PREFIX` in `cendoj.py` strips only `Resumen Automático:`, so the
uppercase label reaches the caller glued to the summary.

What does not filter by subject:

```
MATERIAS=TRÁFICO DE DROGAS                          -> baseline, byte-identical
MATERIAS=TRÁFICO DE DROGAS GRAVE DAÑO A LA SALUD    -> baseline, byte-identical
MATERIAS=INVENTADA                                  -> baseline, byte-identical
texto='"tráfico de drogas"'                         -> same 8/10 as unquoted
texto='+tráfico +drogas'                            -> worse, 6/10
texto='tráfico AND drogas' / 'tráfico Y drogas'     -> same as unquoted
voces="TRÁFICO DE DROGAS" + Melilla + SENTENCIA     -> 185 hits, misses 53/2026 and 38/2026
```

Recall, measured:

```
texto='tráfico de drogas'  -> 8 of the top 10 are drug-labelled, the other two
                              are a drink-driving and an extranjería resolution
plain Melilla + SENTENCIA  -> the 50 most recent are all dated 2026-05-06 or
                              later, so no drug resolution appears at all
four wordings unioned      -> complete for the window (only 2026-01-28 was new)
```

### Task 1 verification (work unit `0cb9407`)

- Test counts: 195 passed + 1 skipped before the work unit, 211 passed + 1
  skipped after, with no test removed.
- Live acceptance, through the tool function against the site: the Melilla drug
  query returns `total=200`, `total_capped=true`, and a `materia` per result --
  `'TRÁFICO DE DROGAS GRAVE DAÑO A LA SALUD'`, `'SOBRE SUSTANCIAS NOCIVAS PARA
  LA SALUD'`, and `'LESIONES POR IMPRUDENCIA'` for the resolution the free text
  dragged in. That last one is the point of the field: the caller sees the
  site's classification instead of inferring the subject from its own wording.
- Independent verification checked the property by hand across nine inputs
  (missing summary, both markers, lowercase markers, an empty label, the
  `Resumen Automático:` decoy, trailing spaces) and found four missing
  assertions, all closed before the commit: `total_capped` was never asserted to
  be in `SearchPage.as_dict()`, the markers were only tested in uppercase, the
  `MAX_RESULTS` import surface was unpinned, and the empty-label and decoy cases
  had no test.
- Process error worth recording: the parent ran `git stash` in parallel with the
  read-only verifier, so two of that verifier's pytest counts measured `HEAD`
  (34 and 195) instead of the worktree. Do not stash while a verifier is reading
  the tree; the parent's own counts are the authority.

### Task 2 verification (work unit `7e416e9`)

- Test counts: 211 passed + 1 skipped before the work unit, 213 passed + 1
  skipped after, with no test removed.
- The description was written to be read on every call, so it stays short: the
  recipe, the classifier rule, the ceiling and its remedy, the absence of a
  server-side subject filter, and the four accepted page sizes.
- The first version of the recipe test asserted bare substrings (`"10" in
  description`), which any stray digit satisfies. Caught before the commit and
  replaced with the load-bearing phrases, whitespace-collapsed so line wrapping
  does not break them: the page sizes as enumerated, the recipe naming
  `localizacion` and `texto`, `materia` as the classifier, `total_capped` tied
  to the 200-record ceiling, and the `voces` caveat.

### Task 3 verification (work unit `a46c368`)

- Test counts: 213 passed + 1 skipped before the work unit, 240 passed + 1
  skipped after, with no test removed from either touched file.
- Live acceptance through the tool function: `COMUNIDAD` returns 19 tokens
  starting `ANDALUCÍA(C)`; `PROVINCIA` with `comunidad=MELILLA` returns
  `("MELILLA(P)",)`; `SEDE` with both parents returns `("MELILLA(S)",)`; the
  alias `nivel="C"` resolves to the comunidad level; and a missing parent
  raises `ValueError`.
- Independent verification exercised the error paths directly (non-success
  payload, non-JSON body, missing `result`, empty `result`), confirmed the wire
  body carries the level WORD rather than the short code and that lowercase
  parents are uppercased, and found no gaps in the five adversarial cases it
  probed. It reported one latent oddity: `result: null` returned an empty tuple
  instead of raising, because `str(None)` has no `&` to split on.
- That oddity was fixed before the commit, and the comunidad parsing test was
  strengthened from a membership check to the full 19-token tuple: a parser that
  dropped or duplicated entries would have passed the loose version.

## Close

Closed on 2026-09-20 on branch `feat/cendoj-search`. Work units `0cb9407`
(the data), `7e416e9` (the guidance) and `a46c368` (the vocabulary tool), with
their evidence commits `42cdb22` and `64e2e8d` and the closing update.

Verified outcome:

- The subject the site assigns is now a field, not a string to parse:
  `materia` carries the label, `null` for the two unclassified markers. Verified
  live through the tool on the query that started this: the drink-driving
  resolution the free text drags in now arrives labelled
  `'LESIONES POR IMPRUDENCIA'`, next to the drug-trafficking ones, so the caller
  can tell them apart without re-reading the query.
- The 200-record ceiling is visible: `total_capped` reports when the total is a
  ceiling rather than a count.
- The recipe lives in the tool description, so the caller no longer has to
  discover by probing that free text is fuzzy, that the subject axis is not
  filterable, or that only four page sizes exist.
- Place tokens are no longer guessed: `listar_localizaciones` returns the site's
  own vocabulary as ready-to-use tokens (19 comunidades, `MELILLA(P)`,
  `MELILLA(S)`), and a missing parent raises.
- Final suite: 240 passed, 1 skipped. No test was removed in any work unit.
- Measured effect on the question that motivated the feature: it used to take
  about eight calls and could still be wrong, because the filter that looks
  right (`voces`) misses two of the five answers. Now one call returns the
  subject-classified results and says whether the set was truncated, so a second
  call is only needed when `total_capped` is true.

Checks that did not pass, or were skipped:

- Native review: not run for this slice. See the next section; this facade
  reviews the workspace projection in `ordinary` mode and its controller rejects
  the explicit `baseRef` form, so committed work has no candidate.
- `ver_texto_completo` is untouched and still needs a human for the site's
  captcha.
- Live tests stay opt-in (`NAVAJA_LIVE=1`).
- `navaja-mcp` does not hot reload: every source change needs a restart after
  the commit that carries it. Three of the four changes here touch `src/`, so a
  restart is required before they can be observed through the MCP.
- Process error recorded during task 1: the parent ran `git stash` in parallel
  with a read-only verifier, invalidating two of that verifier's pytest counts.
  The parent's own counts were used instead. Do not stash while a verifier is
  reading the tree.

Next step:

- Nothing pending for this feature. Open items, all out of scope and measured as
  not working: any server-side subject filter, an internal wording-union tool,
  and the 6481-entry `materiasleg` vocabulary.

## Notes

- `records_por_pagina` accepts only 10, 20, 30, 50, so "the last 5" means asking
  for 10 and slicing. The tool description says so, which removes a probe that
  currently ends in a `ValueError`.
- The site's own search UI sorts by entry date (`IN_FECHAENTRADA:numberdecreasing`,
  set in `functions.js`), while navaja sorts by resolution date. Resolution date
  is what "the latest" means to a researcher, so the difference is documented
  rather than changed.
