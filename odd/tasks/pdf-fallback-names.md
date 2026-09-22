# Readable PDF fallback names

Issue #4: when the server sends no usable `name=` in the `Content-Type` header,
navaja falls back to `<reference>_<optimize>.pdf`, which is a URL hash. A
directory listing then reads:

```
STS_4209_2020.pdf                                ← readable, server-sent
0217b6148cd22fb2a0a8778d75e36f0d_20251210.pdf    ← hash fallback
c90217857e4a1db9_20210421.pdf                    ← hash fallback
```

Both downloads are correct; only the name is useless. Four of the 33 PDFs in a
real working directory fell back this way.

Baseline: `353 passed, 2 skipped`, clean tree at `9daf6ba`.

## What the evidence says

Verified against the 34 PDFs already downloaded to `~/.local/share/navaja/pdfs/`
(see the reconnaissance recorded for this issue):

1. **The ROJ is always present in the extracted text**, in the header block — not
   on the first line, which is always `JURISPRUDENCIA`:
   ```
   JURISPRUDENCIA
   Roj: STS 5365/2025 - ECLI:ES:TS:2025:5365
   Id Cendoj: 28079120012025100992
   ```
2. **The ROJ uses the same convention as the names the server itself sends.**
   `Roj: STSJ AND 2465/2024` corresponds to `STSJ_AND_2465_2024.pdf`;
   `Roj: SAP ML 110/2026` to `SAP_ML_110_2026.pdf`; `Roj: STS 4209/2020` to
   `STS_4209_2020.pdf`. Deriving the fallback from the ROJ therefore produces
   names in the *existing* style, not a new one.
3. **The text is already in hand where the name is chosen.** `save_pdf` has
   exactly one caller, `save_full_text_pdf`, and `cendoj.py` extracts the text
   from the same bytes it keeps as `pdf_bytes`. No PDF is re-parsed.
4. **A failed extraction excludes itself**: `_extract_pdf_text` returns the
   sentinel `"[PDF extraction failed: …]"`, which cannot match the ROJ pattern.

## Decided semantics

- The fallback preference becomes: **ROJ from the document's own text**, else the
  existing `<reference>_<optimize>.pdf` hash. A server-sent name still wins over
  both, unchanged.
- The ROJ is searched **only in the header window** (the first 2000 characters).
  A judgment citing another judgment carries the cited document's `Roj:` in its
  body, and taking the first match anywhere would name the file after the wrong
  document.
- **No new `pdf_save_reason` value.** Those reasons describe *why the server-sent
  name was rejected* (`missing_name`, `unsafe_name`, `overlong_name`), not how the
  fallback was built, so improving the fallback does not touch that vocabulary.
  That vocabulary is a published contract: `server.py`, `README.md` and roughly
  fourteen tests pin it.
- `text` reaches `save_pdf` as an **optional keyword**, so its existing callers
  and tests keep working unchanged.

## Tasks

- [x] 1. Derive a fallback filename from the document's ROJ, sanitized into the
      same `TIPO_SEDE_NUM_AÑO.pdf` shape the server uses, searched only within the
      header window.
- [x] 2. Thread the extracted text into `save_pdf` as an optional keyword and pass
      it from `save_full_text_pdf`.
- [x] 3. Test the derivation against the real header strings measured from the
      downloaded PDFs, including the four that fell back to hashes.
- [x] 4. Test the cases that must keep falling back to the hash: no ROJ, the
      extraction-failure sentinel, an empty or unusable ROJ, and a `Roj:` that
      appears only in the body beyond the header window.
- [x] 5. Confirm a server-sent name still wins over the ROJ, and update the README
      sentence that documents the fallback.
- [x] 6. Verify on Linux, then push so both CI jobs measure it.

## Evidence

Suite: `353 passed, 2 skipped` -> `367 passed, 2 skipped`. The skip set is
unchanged: `-rs` reports only the two opt-in `live` tests.

**The decisive check ran against the real documents, not fixtures.** Every one of
the 34 PDFs in `~/.local/share/navaja/pdfs/` was put through
`_extract_pdf_text` and then the new derivation:

- The four files that used to fall back to a bare hash now derive readable names:
  `0217b6148cd22fb2a0a8778d75e36f0d_20251210.pdf` -> `STS_5365_2025.pdf`,
  `60d13aa0db05f4e6_20210514.pdf` -> `STS_1489_2021.pdf`,
  `c90217857e4a1db9_20210421.pdf` -> `STS_1301_2021.pdf`,
  `f726f0413ebad026a0a8778d75e36f0d_20240805.pdf` -> `STS_4268_2024.pdf`.
- **Every already-readable file reproduces its own name on disk.** The derivation
  agrees with the naming the server itself sends, across all 29 of them, which is
  a stronger check than any fixture: it means the derived shape is not merely
  plausible, it is the same one.
- The single document that derives nothing is
  `aabbccddeeff00112233445566778899_20260911.pdf`, a 25-byte fake-PDF stub the
  test suite itself uses. Its text is the extraction-failure sentinel, so falling
  back to the hash is the correct outcome, not a miss.

- **The header window is load-bearing**: raising `_HEADER_WINDOW_SIZE` makes the
  beyond-window test fail with the cited document's ROJ-derived name
  (`STS_5365_2025.pdf` instead of the hash), which is exactly the wrong-document
  outcome the window exists to prevent.
- **The published reason vocabulary is untouched**: no value added, none renamed.
  The preference order is pinned by three tests (server name over ROJ, ROJ over
  hash, hash when no usable ROJ).
- **The ROJ extraction does not require the ECLI suffix.** An earlier draft matched
  only `Roj: … - ECLI:…`, which meant a document without that suffix would fall
  back to the hash — a silent return of the very bug this change removes. The
  pattern now matches the whole `Roj:` line within the window and strips a trailing
  ECLI clause, so the clean name is unchanged where the suffix exists and is still
  derived where it does not.

Deliberately **not** verified here: anything about Windows. The two CI jobs are
the measurement.
