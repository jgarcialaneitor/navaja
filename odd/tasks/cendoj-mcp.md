# Feature: cendoj-mcp

MCP server for personal CENDOJ case-law research.

## Intent

Ask navaja for case law of interest, get candidates proposed with metadata and
automatic summaries, and for the ones worth reading, retrieve the full text of
the resolution.

## Scope

In scope:

- Search CENDOJ by free text.
- Return structured metadata per resolution: ROJ, ECLI, date, organ, seat,
  resolution number, appeal number, municipality, ponente, and the site's own
  automatic summary.
- Retrieve the full text of a resolution the user explicitly selected.

Out of scope, deliberately:

- Any automatic solving of the site's `Control Descargas masivas` captcha
  (`/search/stickyImg`). Full-text retrieval is human-in-the-loop: when the
  site returns its captcha page, navaja starts a short-lived local HTTP form
  on a private interface, the human opens that URL on whatever device has a
  browser, and the user types the answer. Rationale recorded in Engram: the
  captcha is the control the CGPJ installed on purpose, the challenge is
  session-sticky (roughly once per research session), and an unattended solver
  would move this project from operating within the site's terms to operating
  around them.
- Mass download, caching or redistribution of resolutions.
- Commercial use or reuse of the database.

## Constraints

- Personal use only, as permitted by the site's terms.
- Polite by default: deterministic tests run against saved fixtures, never
  against the live site. Live tests are opt-in via `NAVAJA_LIVE=1`.
- The human-in-the-loop step must bind a specific interface. `0.0.0.0`, `::`
  and empty hosts are refused; use loopback or a private network such as
  Tailscale. It therefore works on a headless host.

## Tasks

- [x] 1. Scaffold the project: `pyproject.toml`, `src/navaja` layout, dev
      dependencies, README.
- [x] 2. Parse CENDOJ search results into a domain model.
- [x] 3. HTTP client for the CENDOJ search endpoint, with session bootstrap.
- [x] 4. Deterministic tests over a saved results fixture, plus an opt-in live
      smoke test.
- [x] 5. Verify pagination semantics against the live site.
- [x] 6. MCP server exposing `buscar_sentencias` and `ver_texto_completo`.
- [x] 7. Full-text step: request the document, serve the captcha on a
      short-lived local form when required, let the user solve it, and extract
      the text.
- [x] 8. Wiring docs: how to register the MCP server and run it locally.
- [x] 9. Zero-configuration captcha form:
      - Auto-detect the captcha bind interface: `NAVAJA_CAPTCHA_HOST`, then
        `tailscale0` IPv4, then `127.0.0.1`. Reject `0.0.0.0`, `::` and empty
        hosts. The detection is unit-testable via an injectable interface list
        and uses the standard library (`socket`/`fcntl`), not a new dependency
        and not the `tailscale` binary.
      - Stable, persisted captcha token: `NAVAJA_CAPTCHA_TOKEN`, then
        `$XDG_STATE_HOME/navaja/captcha-token` (default
        `~/.local/state/navaja/captcha-token`), then generate with
        `secrets.token_urlsafe(32)` and persist. Directory mode `0700`, file
        mode `0600`, atomic write (temp file + `os.replace`). Malformed files
        are regenerated; the token value is never logged in isolation.
      - Apply both defaults in `server.py` and `cli.py`; announce the chosen
        host on stderr together with the form URL.
      - Add offline tests for host resolution, token persistence, file modes,
        malformed-file recovery and concurrent-safe writes.
      - Verified: `78 passed, 1 skipped` (`uv run pytest`).
- [x] 10. Fix the reproducible socket leak in `serve_captcha`:
      `server.shutdown()` stops the `serve_forever` loop but does not close
      the listening socket, so the port stayed bound until garbage collection.
      Added `server_close()` in the `finally` block on every exit path
      (success, timeout, exception while waiting, exception in handler) and
      a clear `RuntimeError` when binding fails with `EADDRINUSE`. Added
      regression tests: three consecutive timeouts on the same fixed port
      without `gc.collect()`, successful answer releases the port, handler
      exception still releases the port, and occupied-port bind error is
      actionable. Verified: `96 passed, 1 skipped` (`uv run pytest`).
- [ ] 11. Fix the search-result parser gap for Tribunal Supremo rulings:
      `Sentencia.sede` is `None` for STS/ATS rulings because `_parse_title`
      expects the `"<TIPO> <SEDE>, a <fecha>"` shape used by SAP titles. The
      Supreme Court title does not include the seat in that position, but the
      full-text PDF already carries `Sede`, so the gap is fillable from the
      document as well as from the title.

## Evidence

- Tasks 1-5 closed. Intermediate checkpoint: `19 passed, 1 skipped`
  (`uv run pytest`).
- Live smoke test against the real site: `1 passed`
  (`NAVAJA_LIVE=1 uv run pytest -m live`).
- Pagination verified live: page 1 -> `SAP  NA 1461/2026` (ref 11851652),
  page 2 -> `STS 3679/2026` (ref 11853362). Results differ, so `start=<page>`
  is the working pagination parameter.
- `.gitignore` now covers `__pycache__/`, `*.py[cod]`, `.venv/`, `venv/`,
  `*.egg-info/`, `.pytest_cache/`, `.uv/`, `.navaja-profile/` and `.engram/`.
  Before this, `git add -A` staged `src/navaja/__pycache__/*.pyc` and
  `tests/__pycache__/*.pyc`.
- Task 7 closed. Added:
  - `src/navaja/captcha.py`: local human-in-the-loop captcha form server
    (`serve_captcha`) that binds only loopback, refuses `0.0.0.0`/`::`/empty
    hosts, uses a random token-protected URL, serves the image and form, and
    returns the typed answer.
  - `src/navaja/cendoj.py`: `CendojClient.fetch_full_text` is now a real
    method on the class. It imports helpers from `src/navaja/documents.py`
    and `serve_captcha` from `src/navaja/captcha.py`.
  - `src/navaja/documents.py`: `parse_document_url`, `DocumentRef`,
    `FullTextResult`, `FullTextError`, and the captcha helper functions.
    No longer imports `CendojClient`, breaking the circular dependency.
    `FullTextResult.attempts` now counts human captcha attempts; `requests`
    counts HTTP requests (initial GET plus each captcha POST).
  - `src/navaja/cli.py`: `navaja-doc` console script for immediate manual use.
  - `tests/test_captcha.py` and `tests/test_documents.py`: deterministic,
    offline coverage of URL parsing, form POST body, wrong-answer path, PDF
    detection by magic bytes, and HTML fallback stripping.
- `pyproject.toml` now depends on `pypdf>=5.0` and registers the `navaja-doc`
  console script.
- Verified offline with `uv run pytest`: intermediate checkpoint `47 passed,
  1 skipped`, including all pre-existing tests.
- Task 8 closed. The task was previously marked complete even though the
  README contained no MCP registration or local-run instructions. This update
  rewrites `README.md` to match the implementation and adds the missing
  wiring documentation, so the `[x]` is now honest.
- Final full suite: `59 passed, 1 skipped` (`uv run pytest`).
- `pyproject.toml` depends on `mcp>=2,<3` and registers the `navaja-mcp`
  console script.
- `src/navaja/server.py` implements the MCP server with:
  - `buscar_sentencias(texto, pagina=1, records_por_pagina=10) -> dict`
  - `ver_texto_completo(url, espera_segundos=300) -> dict`
  - `estado_servidor() -> dict`
- Shared `CendojClient` is created lazily under `threading.Lock` and reused
  across tool calls; `close_shared_client()` releases it and is registered with
  `atexit`.
- Stdout purity is enforced by wrapping the full-text call in
  `contextlib.redirect_stdout(sys.stderr)` so stray prints from the client go
  to stderr, not the JSON-RPC stdio stream.
- Captcha host is validated (`0.0.0.0`, `::` and empty rejected); port and an
  optional stable token are read from env vars.
- The stable `NAVAJA_CAPTCHA_TOKEN` is forwarded directly to
  `serve_captcha(..., token=...)`; there is no monkey-patching of
  `secrets.token_urlsafe`. Supplied tokens are validated as URL-path-safe
  (`A-Z`, `a-z`, `0-9`, `-`, `_`, minimum 16 characters).
- Verified: the live CENDOJ captcha round-trip was exercised with
  `uv run navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917"`.
  The site returned `Content-Type: application/pdf; name="STS_3679_2026.pdf"`,
  the flow succeeded on the first attempt, and `pypdf` extracted clean text
  including `JURISPRUDENCIA Roj: STS 3679/2026`, `Id Cendoj:
  28079110012026101379`, and the richer metadata fields (`Órgano`, `Sede`,
  `Sección`, `Fecha`, `Nº de Recurso`, `Nº de Resolución`, `Procedimiento`,
  `Ponente`, `Tipo de Resolución`). The human reached the local form through
  an SSH tunnel, which runs over port 22 and therefore needed no `ufw`
  change.
- Commit: `4899472e7f94592bd800fba24f7bd1450569b908` — "feat: add navaja, an
  MCP server for personal CENDOJ research" (18 files, 4093 insertions).

## Notes

- Endpoint map established by read-only reconnaissance:
  - `GET /search/indexAN.jsp` bootstraps the `JSESSIONID` cookie.
  - `POST /search/search.action` with `action=query`, `databasematch=AN`,
    `TEXT`, `sort`, `recordsPerPage`, `start`. No captcha.
  - Results carry `div.searchresult.doc[data-ref]`, with `.title a[data-roj]`,
    `.metadatos li` and `.summary` (automatic summary).
  - `HEAD` requests return HTTP 500; only `GET`/`POST` are supported.
- Socket lifecycle fix (task 10): the `finally` block in `serve_captcha`
  now calls `server.shutdown()`, `thread.join()` and `server.server_close()`,
  preserving any in-flight exception. Occupied ports raise a clear
  `RuntimeError` naming the host and port. Regression coverage in
  `tests/test_captcha.py`.
