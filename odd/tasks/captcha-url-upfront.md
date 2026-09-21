# Feature: captcha-url-upfront

Make the captcha form URL reachable *before* the fetch blocks, and settle a
token policy that two commits left contradicting each other.

## Intent

An agent cannot hand the human the captcha form URL until the blocking fetch
has already burned its whole `espera_segundos`. The URL is only ever returned
on the `captcha_timeout` error path (`cc1a84b`), never up front. In live use
this cost roughly nine minutes across three attempts (180 s + 240 s + 120 s):
the agent had no URL to give while the wait was running, and the MCP session is
sequential, so it cannot announce anything mid-wait.

At the same time the repository states two opposite policies. `cc1a84b`
returns the full form URL, token included, in the timeout payload. `f81da4a`
masks it in `estado_servidor` "without the tool ever handing back the token".
The secret is exposed on one path and hidden on the other, and the hiding buys
nothing: any local process reads the token from
`~/.local/state/navaja/captcha-token`, which is exactly how it was obtained in
the session that produced this task.

The ODD design summary for `captcha-persistent-listener` already specified the
intended behaviour — "`estado_servidor` reports the form URL so the human can
retrieve it without reading stderr" — and `f81da4a` drifted from it. This
feature realigns the code with that specification. The drift is deliberate and
aware: it reverts part of `f81da4a`, and the revert is one field.

## Scope

In scope:

- `estado_servidor` returns the real, usable form URL (`captcha_url`) instead of
  a masked placeholder.
- `ver_texto_completo` returns a structured `captcha_busy` failure carrying the
  form URL when a challenge is already pending, instead of letting
  `CaptchaBusyError` escape as an internal error.
- A shorter default `espera_segundos`, documented against the client's request
  timeout.
- README and tool-description updates for the policy and for the
  open-once-and-leave-it flow.

Out of scope, deliberately:

- Any non-blocking register/solve/collect model, a challenge slot that survives
  a call, a challenge queue, or concurrent challenges. Recorded as a follow-up
  proposal, not implemented here: making the slot outlive the call reopens the
  stale-answer window that `captcha-persistent-listener` closed with
  per-challenge ids, and that deserves its own risk budget.
- Any automatic captcha solver, OCR, vision model or third-party service.
  Unchanged and non-negotiable.
- The Pi-side session drop and its misleading "Expected parameters: url
  (string) *required*" message. That is harness behaviour, not navaja's. Only
  the navaja-side conditions that trigger it are addressed here.
- Host binding, token validation, the idle page, and per-challenge id
  validation.

## Constraints

- No test may open the live CENDOJ site; the `live` marker stays deselected by
  default.
- `server.py` must not import `secrets` or call `token_urlsafe` (locked by
  `tests/test_server.py`).
- `server.py` must not bind a socket at import time.
- stdout stays pure JSON-RPC; every human-facing message stays on stderr.
- `CaptchaBusyError` already exists in `captcha.py`; it must be caught where it
  reaches the tool boundary, not silenced at its source.

## Design summary

- `estado_servidor` builds the form URL from the resolved host, port and token,
  mirroring `CaptchaServer.url`, and drops `captcha_url_masked`. One field, one
  meaning.
- `ver_texto_completo` catches `CaptchaBusyError` alongside `CaptchaTimeoutError`
  and returns the same payload shape with `error_code: "captcha_busy"` plus
  `captcha_url`.
- The default `espera_segundos` drops from 300 to 120, with the relationship to
  the client's request timeout stated in the tool description and the README.

## Tasks

- [ ] 1. `server.py`: `estado_servidor` returns the real `captcha_url`;
      `ver_texto_completo` catches `CaptchaBusyError` into a structured
      `captcha_busy` payload; default `espera_segundos` 300 -> 120; update the
      affected tests in `tests/test_server.py`, including the one that locks the
      masking.
- [ ] 2. Documentation: README captcha section (policy, open-once-and-leave-it
      flow, request-timeout relationship) and the two tool descriptions.

## Evidence

Baseline before this feature: `.venv/bin/python -m pytest` -> `280 passed,
1 skipped in 22.34s`, on branch `feat/cendoj-search`, clean tree.

### Work unit 1 — `src/navaja/server.py` + `tests/test_server.py`

- New and changed tests, red then green:
  `estado_servidor_returns_real_captcha_url`,
  `lifespan_starts_and_stops_listener`, `lifespan_url_contains_real_token`,
  `ver_texto_completo_busy_returns_structured_failure`,
  `ver_texto_completo_default_wait_is_120` -> `5 failed, 54 deselected` (red)
  then `5 passed, 54 deselected` (green).
- Full suite after this unit: `282 passed, 1 skipped`.

Independent verification (separate read-only agent), round 1 — four claims
confirmed, three findings raised. Confirmed against real infrastructure rather
than by reading code: the `captcha_url` from `estado_servidor` is byte-identical
to the URL the bound listener serves, a real GET with no challenge pending
returns 200 with the idle page, and a wrong token returns 404;
`CaptchaBusyError` cannot escape the tool boundary and the `captcha_busy`
payload carries the same key set as `captcha_timeout`, including the three PDF
keys; the registered tool schema reports `espera_segundos` default 120;
`server.py` imports no `secrets`, binds no socket at import, and stdout stayed
pure JSON-RPC through a real stdio handshake. Findings:

1. Regression, fixed in work unit 1: moving `resolve_captcha_token()` above the
   `default_captcha_token_path().exists()` check made `stable_token_set`
   vacuously true, because resolving a missing token creates and persists it.
2. New side effect, accepted and documented: `estado_servidor` is no longer
   side-effect free; it persists a token (file `0600`, directory `0700`) when
   none exists. Only observable outside a real MCP session, which already
   resolves the token during lifespan startup.
3. README drift: `README.md` still documented `captcha_url_masked` and
   "deliberately never returns the token". Addressed by work unit 2.

### Work unit 2 — `README.md`, plus the correction to work unit 1

- Regression test `test_estado_servidor_stable_token_set_reports_pre_call_state`
  -> `1 failed` (red, `assert True is False`) then `1 passed` (green).
- Full suite after this unit: `283 passed, 1 skipped`.
- README now documents the real `captcha_url`, the open-once-and-leave-it flow,
  the 120 s default and the `captcha_busy` code; no stale reference to the
  removed behaviour remains in `src/`, `tests/` or `README.md`.

Independent verification, round 2 (re-judgment after the correction) — all five
claims confirmed, no previously verified fact regressed. `stable_token_set` is
genuinely fixed, proven with an isolated empty `XDG_STATE_HOME`: first call
`False` with a well-formed URL, token file created `0600` inside a `0700`
directory, second call `True`, and the URL identical across both calls. Every
factual claim in the new README text matches the implementation; the five error
codes listed are exactly the five the code can emit, and `captcha_url` appears
in exactly the two payloads the README names. Suite reproduced at `283 passed,
1 skipped`.

Red-phase reproduction by the parent, before any commit: with the new tests in
place and only `src/navaja/server.py` reverted to `HEAD`, the six tests that pin
this feature fail — `6 failed, 54 deselected in 2.71s` — with the causes the
tests are supposed to catch: `KeyError: 'captcha_url'` for the renamed field,
`assert 300.0 == 120.0` for the default wait, and an uncaught
`CaptchaBusyError` for the busy path. Restoring the file returned it
byte-identical (sha256 checked) and the suite came back to `283 passed,
1 skipped`. The red-then-green pair is therefore reproducible on demand, not
merely reported by the writer.

Post-verification edit, documentation only and below the re-verification bar:
the 330 s figure was presented as the universal MCP client timeout when it is
this deployment's `~/.pi/agent/mcp.json` value. Softened in both `README.md`
and the `ver_texto_completo` docstring. No behaviour change; suite re-run green
at `283 passed, 1 skipped in 23.12s`.

Follow-up verification round 3, covering exactly those two post-verification
edits: five claims confirmed (`283 passed, 1 skipped`; the registered tool
schema still reports `espera_segundos` default 120 with `url` the only
required parameter; `estado_servidor` still returns a URL that a real HTTP GET
serves with 200; nothing else in live documentation presents 330 s as
universal; both committed slices touch exactly the paths they claim, with a
clean tree). It raised one wording defect, now corrected: 'in the default Pi
configuration' implied that 330 s was Pi's shipped default, and it is not. It
is a per-server value in `~/.pi/agent/mcp.json` (`bome-melilla` in the same
file sets `requestTimeoutMs: 30000`), and the MCP adapter falls back to the MCP
SDK default when the value is absent. The wording now names the config file
directly. Round 3 also corrected the line below.

Committed range `84ce698..HEAD`: `README.md`, `src/navaja/server.py`,
`tests/test_server.py` and this feature document. An earlier version of this
line said '3 files changed, 179 insertions', which silently excluded this
document itself.

### Work unit commits

- Work unit 1 — `feat(mcp): expose the real captcha form URL and a structured
  busy failure` (`src/navaja/server.py`, `tests/test_server.py`).
- Work unit 2 — `docs: document the real captcha URL and the structured
  failures` (`README.md`, plus this feature document).
