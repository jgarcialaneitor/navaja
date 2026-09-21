# Feature: pdf-download

Persist the resolution PDF that navaja already downloads, so a full-text fetch
leaves a reusable file on disk instead of only a transient string.

## Intent

`CendojClient.fetch_full_text` already holds the complete PDF in memory:
`cendoj.py` populates `FullTextResult.pdf_bytes` whenever the final response is
a PDF, and `tests/test_documents.py` locks that behaviour. Nothing ever writes
those bytes anywhere. The MCP tool payload in `server.py` copies `ok`,
`attempts`, `requests`, `content_type` and `text` and drops the rest, so the
bytes die with the call. The CLI `--out` flag writes `result.text` in text
mode, which is the extracted text, never the document.

In live use this means every consultation is one-shot: re-reading a resolution
costs another HTTP round trip and, on a cold session, another human captcha.
Large texts also overflow the MCP output limit and get spilled into a truncated
temp file, which is a poor archive of a document the process already had whole.

The PDF is the citable artifact. Keeping it is the point.

## Decisions

Both settled with the user before implementation:

- **When to save**: always, whenever the response actually is a PDF, to a
  configured directory. No per-call opt-in parameter.
- **How to name**: use the filename the server itself sends in the
  `Content-Type` header (`name="SAP_ML_110_2026.pdf"`), which is the official,
  human-readable name. It is third-party input, so it must be sanitized before
  it reaches the filesystem.

## Scope

In scope:

- A destination resolver following the existing `resolve_captcha_host` /
  `resolve_captcha_token` shape: return `(path, reason)`, honour
  `NAVAJA_PDF_DIR` first, then an XDG-based default, and create the directory
  on demand.
- A filename derived from the server-sent `name=` parameter, sanitized to a
  bare basename, with a deterministic fallback built from `DocumentRef` when
  the header is absent, unusable, or sanitizes to nothing.
- Collision handling that never silently overwrites a different document.
- `ver_texto_completo` returning the written path in its payload, plus a
  reason/skip field so the caller can tell "saved here" from "nothing to save".
- CLI parity so `navaja-doc` also leaves the PDF on disk.
- README documentation of the new environment variable, the default location,
  and the naming rule.

Out of scope, deliberately:

- Returning PDF bytes (base64 or otherwise) through the MCP payload. The
  transport is JSON over stdio and the text alone already overflows the output
  limit; the path is the contract.
- Any change to captcha handling, host/token resolution, or network logic.
- A cache layer that skips the HTTP request when the file already exists. This
  feature persists; it does not change fetch semantics.
- Retention, pruning or size limits for the download directory.

## Constraints

- **Path traversal is the main risk.** The filename comes from the remote site.
  A `name="../../.ssh/authorized_keys"` must never escape the destination
  directory. Reduce to a basename, reject separators and `..`, and verify the
  resolved path stays inside the destination.
- PDF bytes must be written in binary mode. The existing `--out` path uses
  `open(..., "w", encoding="utf-8")`; the PDF writer must not copy that.
- A write failure (permissions, full disk, bad `NAVAJA_PDF_DIR`) must not fail
  the fetch. The human already paid a captcha for that response: report the
  write problem in the payload and still return the text.
- The stdio JSON-RPC stream stays clean. Every human-facing message goes to
  stderr.
- Deterministic tests only. No test may open the live CENDOJ site, and no test
  may write outside `tmp_path`.
- Non-PDF responses (HTML) produce no file and say so explicitly.

## Tasks

- [x] 1. Destination resolver and safe PDF writer, with tests covering
      traversal attempts, absent/garbage header names, collisions, non-PDF
      responses, and write failures.
- [x] 2. Wire into `ver_texto_completo`: save on success, expose the path and
      the skip/failure reason in the payload, keep `text` unchanged.
- [ ] 3. CLI parity and README documentation of the variable, default path and
      naming rule.

## Evidence

### Task 1

Public API added to `src/navaja/documents.py`:

- `resolve_pdf_destination() -> tuple[Path, str]` — `NAVAJA_PDF_DIR`, then
  `$XDG_DATA_HOME/navaja/pdfs`, then `~/.local/share/navaja/pdfs`.
- `PdfSaveResult(ok, path, reason, error)` — frozen, slotted.
- `save_pdf(pdf_bytes, content_type, ref, destination) -> PdfSaveResult`.

Reason vocabulary. Saved: `server_sent_name`, `missing_name`, `unsafe_name`,
`overlong_name`, `identical_bytes`. Not saved: `empty_payload`, `not_pdf`,
`mkdir_failed`, `path_escape`, `directory_collision`, `name_too_long`,
`write_failed`.

Two defects were found by verification after the first implementation pass and
fixed before closing:

1. Unguarded `exists()` / `read_bytes()` probes in the collision path raised
   `OSError` instead of returning a result. A 255+ character server-sent
   `name=` was remotely triggerable and aborted the fetch after the captcha
   was already paid. Probes are now guarded and mapped to reasons.
2. That fix initially made an overlong name fail closed, losing the document.
   Overridden: an overlong name now degrades to the deterministic fallback and
   the file is written, consistent with how `unsafe_name` already behaved in
   the same function. The length check is deterministic (`PC_NAME_MAX`, 255
   fallback) and runs before the containment check.

Verification: `uv run pytest` → 264 passed, 1 skipped (265 collected). Baseline
before the feature was 262. Traversal vectors executed by the verifier and
refused: `../../evil.pdf`, absolute path, NUL byte, leading `~`, RFC 2231
percent-encoded separators, RFC 2231 continuations, backslash separators,
unquoted and case-varied parameters, and a pre-existing symlink inside the
destination pointing outside it.

Known residual, accepted: a TOCTOU symlink race between the collision probe
and `write_bytes` requires a local attacker who already has write access to a
user-owned directory, so it grants no privilege.

Committed as `2f644c7`.

### Task 2

`ver_texto_completo` now saves automatically whenever the fetched response is
a PDF, and the payload always carries three more keys: `pdf_path`,
`pdf_save_reason` and `pdf_save_error`. The keys are present on every return
path, so callers never probe for them.

`pdf_save_reason` separates three situations that the first implementation
conflated under `not_pdf`:

- `not_attempted` / no response was ever retrieved (`invalid_url`,
  `captcha_timeout`, `captcha_rejected`, `full_text_error`).
- `not_pdf` / a response arrived and was not a PDF.
- `resolve_failed` / the destination could not be resolved.

A save problem never downgrades the fetch: `ok` stays `True` and `text` is
returned in full. The verifier executed three hostile `NAVAJA_PDF_DIR` values
against the real code path / a path that is a file, a target under a read-only
parent, and a `chmod 000` directory / and all three degraded to a reported
reason with the complete text intact and an empty stdout.

Verification: `uv run pytest` \u2192 273 passed, 1 skipped.

Native review: lineage `review-db439f8918bb7502`, tier medium, lens
`review-reliability`, approved and acknowledged (authority burned).

### Follow-ups from the approved review

All non-blocking advisories, recorded here as separate later work:

- `src/navaja/server.py:467-482` / the `try` wraps both `resolve_pdf_destination`
  and `save_pdf`, so an unexpected exception from the latter would be reported
  as a destination-resolution failure. Narrow the guard.
- `src/navaja/server.py:476` / `pragma: no cover` sits on a branch that a test
  actually exercises.
- `src/navaja/server.py:504-506` / the payload derives its keys from `path` and
  `reason` and never reads `PdfSaveResult.ok`.
- `src/navaja/server.py:490-496` / the `not_attempted` branch is reachable only
  through a not-ok `FullTextResult`; add a direct test.
- `tests/test_server.py:69-92` / the PDF fixture has an inconsistent startxref
  pointer, which makes pypdf emit a warning.
- `tests/test_server.py:740` / a path assertion uses a prefix check where a
  containment check is the intended meaning.
