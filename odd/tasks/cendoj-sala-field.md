# Expose the Supreme Court chamber as `Sentencia.sala`

Issue #14: the search parser drops the chamber for Tribunal Supremo rulings, and
`sede` is `None` for every STS/ATS.

## The defect, measured

| Fixture | Results | `sede is None` |
| --- | --- | --- |
| `tests/fixtures/search_ts_coleccion.html` (TS collection) | 20 | 20 |
| `tests/fixtures/search_clausulas_abusivas.html` (AN collection) | 10 | 9 |

The single populated result is the one provincial judgment: `SAP  NA 1461/2026`
-> `sede == "Navarra"`.

`_parse_title` splits the organ line on its first space, which is the shape
provincial titles use:

```python
organo = _clean(organo)
tipo, _, sede = organo.partition(" ")
```

A Supreme Court title carries no seat token between the type and the comma
(`STS, a 07 de septiembre de 2026 - ROJ: STS 3668/2026`), and unlike
`SAP NA 1461/2026` its ROJ carries no seat code either.

The seat is not missing from the page: `Municipio: Madrid` is already captured as
`municipio`. What the parser drops is the chamber, in a bold unlabelled entry:

```html
<li><b>Sala de lo Civil</b></li>
```

`_parse_metadatos` derives the label as `raw[: -len(value)]`, which is empty when
the `<b>` is the whole entry, so no `_META_MATCHERS` needle hits and the datum is
discarded. Measured per result:

| Fixture | `tipo` | bare `Sala` entry | results |
| --- | --- | --- | --- |
| `search_ts_coleccion.html` | `STS` | `Sala de lo Civil` | 12 |
| `search_ts_coleccion.html` | `STS` | `Sala de lo Penal` | 6 |
| `search_ts_coleccion.html` | `ATS` | `Sala de lo Penal` | 1 |
| `search_ts_coleccion.html` | `STS` | `Sala de lo Social` | 1 |
| `search_clausulas_abusivas.html` | `STS` | `Sala de lo Civil` | 6 |
| `search_clausulas_abusivas.html` | `ATS` | `Sala de lo Civil` | 3 |
| `search_clausulas_abusivas.html` | `SAP` | *(none)* | 1 |

Every Supreme Court result carries exactly one such entry; the provincial result
carries none. The chamber varies, so it is a real discriminator.

## Decided semantics

Chosen on the issue: record the chamber as its own field; leave `sede` alone and
document that `None` for a Tribunal Supremo ruling is intended.

| Decision | Why |
| --- | --- |
| A new field, not a filled `sede` | The ROJ of a Supreme Court ruling carries no seat code, and the server's own filename convention agrees (`STS_4209_2020.pdf` has no seat, `SAP_ML_110_2026.pdf` has one). `sede` is documented as the seat printed in the title, so `None` there is correct. |
| The value is the site's own string | `Sala de lo Civil`, not `Civil`. The parser already stores site vocabulary verbatim (`sede` holds `Navarra`, not `NA`), and mapping to the `JURISDICCION` filter values would invent a translation nothing asked for. |
| Captured from the search page, not the document | The entry is on the results page, so no captcha-gated fetch is needed and a search-only caller gets it. |

`sede` remains a published key of `as_dict()` with unchanged meaning; `sala` is
added to the contract, which the test that pins `set(payload)` must reflect.

Baseline: `367 passed, 2 skipped` on Linux at `beba70c`.

## Tasks

- [ ] 1. Add `sala` to `Sentencia` next to `sede`, with a docstring that says what
      it is and that it is only present for Tribunal Supremo rulings; `as_dict()`
      picks it up from `__slots__`.
- [ ] 2. Capture the bold unlabelled `Sala ...` entry in `_parse_metadatos` and
      pass it through in `_parse_search_page`.
- [ ] 3. Test against the real fixtures: the chamber is populated for every STS/ATS
      in both fixtures, the three observed values are covered, and the provincial
      `SAP` result has `sala is None`.
- [ ] 4. Update the contract test that pins `set(payload)` and assert the new key.
- [ ] 5. Correct the `sede` docstring and the README paragraph that presents
      `Sede: Madrid` as the missing piece, and document `sala`.
- [ ] 6. Reconcile task 11 of `odd/tasks/cendoj-mcp.md` with issue #14: its framing
      is superseded by the measurement above.
- [ ] 7. Verify on Linux and push so both CI jobs measure it.

## Evidence

Recorded as tasks close.
