# Exclusive captcha bind on Windows

Issue #17, reported from a real Windows machine: two navaja processes can end
up listening on the same captcha port (`127.0.0.1:8765`), the browser connects
to the one without the pending challenge, and the page always shows *idle*
while the other process blocks until `captcha_timeout`.

## The defect, measured

`netstat` on the affected machine showed two PIDs in `LISTENING` on the same
address, plus the browser's connection to one of them. The cause is in the
inherited socket options, not in the registry:

- `CaptchaServer` extends `HTTPServer`, whose `allow_reuse_address = True`
  sets `SO_REUSEADDR`. On POSIX that flag only permits reuse of `TIME_WAIT`
  sockets. On Windows it **permits hijacking a port that is actively being
  listened on** — so the second bind succeeds instead of failing.
- `_get_or_create_captcha_server` maps `WSAEACCES` (10013) / `WSAEADDRINUSE`
  (10048) to `RuntimeError("address already in use")`, added in the #10 round.
  That guard exists but never fires in this scenario, because the bind does
  not fail. Its comment claims the behaviour "was measured on Windows CI";
  the real machine measured the opposite.

Contributing factor: the token is persisted and the port is fixed
(`NAVAJA_CAPTCHA_PORT`, default 8765), so both instances serve the *same* URL
and nothing tells the user they are talking to the wrong process.

## Decided semantics

Scope agreed on the issue: **fix the root cause only** (option A). The other
three proposals — the per-process nonce self-check in `estado_servidor`, a
batch-cancellation tool, and releasing an abandoned pending challenge — are
filed as separate issues rather than built here.

| Decision | Why |
| --- | --- |
| `allow_reuse_address = False` on Windows only | On POSIX the flag buys `TIME_WAIT` rebinding and cannot hijack an active listener, so it stays. On Windows it is the hijack enabler, so it goes. |
| `SO_EXCLUSIVEADDRUSE` set before bind on Windows | Microsoft's documented server behavior for this exact failure mode: the second bind fails loudly instead of silently splitting connections. |
| The error message gains a recovery hint | The issue's own mitigation ("kill the duplicated process") is what an MCP agent needs to be told. |

`serve_captcha` is lazy — the listener is created only when the site actually
serves a challenge — so a second instance now fails with
`RuntimeError("captcha server cannot bind to ... : address already in use")`
at challenge time, and its batch job fails immediately instead of silently
receiving the browser's connections.

Baseline: `371 passed, 2 skipped` on Linux at `87d2277`.

## Tasks

- [ ] 1. In `CaptchaServer.__init__`, set instance-level
      `allow_reuse_address = False` when `os.name == "nt"` (POSIX keeps the
      inherited `True`), and override `server_bind` to set
      `SO_EXCLUSIVEADDRUSE` before `super().server_bind()` when the platform
      exposes it. POSIX behaviour must be byte-identical.
- [ ] 2. Correct the stale comment in `_get_or_create_captcha_server` and the
      docstring of `test_bind_refusal_on_windows_raises_clear_error`: they
      state Windows raises `WSAEACCES` for an occupied address *with
      `SO_REUSEADDR` set*; the real machine measured a successful hijack
      instead. Record what was actually measured. Keep both winerror codes in
      the mapping.
- [ ] 3. Enrich the mapped `RuntimeError` with a recovery hint (another navaja
      instance is likely holding the port; kill it or set
      `NAVAJA_CAPTCHA_PORT`), and update the exact-match assertion in the
      existing simulated-refusal test.
- [ ] 4. Regression test: two listeners on the same `(host, port)` — the
      second must raise the mapped `RuntimeError` on **both** platforms. On
      Windows this fails before the fix (both sockets carry `SO_REUSEADDR`
      and the hijack succeeds); on POSIX it passes before and after.
- [ ] 5. Simulated-Windows unit tests following the suite's existing
      simulation style: the instance attribute is `False` on `nt` and the
      inherited `True` elsewhere, and `server_bind` sets
      `SO_EXCLUSIVEADDRUSE` before binding when the platform defines it.
- [ ] 6. README (in Spanish): one short note in the captcha section on what a
      second instance now reports and the recovery path.
- [ ] 7. Verify on Linux, reconcile the ficha, and record follow-up issue
      numbers for the deferred proposals.

## Evidence

Recorded as tasks close.
