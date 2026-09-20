# Feature: captcha-persistent-listener

Make the captcha form URL always answer something, so the human never faces a
blank page and never has to guess whether a challenge is pending.

## Intent

Today the captcha form server is single-use: `serve_captcha` binds `(host,
port)`, serves one challenge, and unconditionally closes the socket in its
`finally` block. Between documents nothing listens on the port, so the stable,
bookmarkable form URL returns an empty page or a connection error. That
confused the user in live use ("el último enlace no me mostraba nada") and the
cause was invisible from the browser.

The stable token already made the URL bookmarkable. This makes it *usable at
any time*: the page answers when a challenge is pending, says so plainly when
none is, and refreshes itself so an already-open tab picks up the next
challenge without the human reloading or guessing timing.

## Scope

In scope:

- One long-lived captcha listener per process, bound to the validated private
  interface, started once and reused for every challenge.
- A per-challenge slot (image, deadline, answer, completion signal) that is
  reset per challenge instead of latched once per server instance.
- An idle page served at the form URL when no challenge is pending, with a
  short auto-refresh so an open tab follows the next challenge.
- A clear error when a second challenge is requested while one is pending,
  instead of the current implicit `EADDRINUSE` failure.
- Wiring in the MCP server (start with the session, stop on exit) and in the
  one-shot CLI, with the port released on stop.

Out of scope, deliberately:

- Any automatic captcha solver, OCR, vision model or third-party solving
  service. The human-in-the-loop property is unchanged and non-negotiable.
- A standalone always-on daemon outside the navaja process. The listener
  exists while a navaja process exists; when nothing answers, that means no
  navaja process is running, and the documentation states exactly that.
- Serving more than one concurrent challenge in a single process.
- Any change to `0.0.0.0`/`::`/empty host rejection or to token validation.

## Constraints

- The bind remains single-interface only. `0.0.0.0`, `::` and empty hosts stay
  refused, and the existing tests that lock that must keep passing.
- The token stays required on every path, including the idle page. A wrong
  token is a 404 in both states.
- Opening the port for the whole session is a deliberate, accepted trade-off
  and must be stated in the README, together with the meaning of "nothing
  answers".
- The stdio JSON-RPC stream stays clean: every human-facing message goes to
  stderr. `server.py` must not import `secrets` or call `token_urlsafe`
  (locked by `tests/test_server.py::test_server_does_not_patch_secrets_for_stable_token`).
- `server.py` must not bind a socket at import time: the existing
  `tests/test_server.py` suite exercises tools without any listener running.
- Deterministic tests only. No test may open the live CENDOJ site.

## Design summary

- `CaptchaServer` becomes a real long-lived object: `start()` binds once and
  runs `serve_forever` on a daemon thread; `stop()` shuts down, joins,
  `server_close()` and is idempotent.
- The challenge slot replaces the constructor-time `image_png` and the
  once-latched `_finished`: the handler reads the slot under the lock to decide
  between the form page and the idle page.
- `serve_captcha(...)` keeps its current signature, return type
  (`CaptchaAnswer`) and blocking semantics, but is reimplemented as
  attach-or-start on a process-wide listener keyed by `(host, port)`. It no
  longer closes the listener.
- `stop_shared_captcha_server()` mirrors the existing `close_shared_client()`
  pattern, with an `atexit` backstop.
- `server.py` starts the listener through `MCPServer(lifespan=...)`, which is
  entered once per `run()` for the stdio transport and exited when the session
  ends.
- `estado_servidor` reports the form URL so the human can retrieve it without
  reading stderr.

## Tasks

Cut by module, not by layer: a task that changes the listener internals but
leaves `serve_captcha` half-migrated would not close on a green commit.

- [x] 1. `captcha.py`: long-lived `CaptchaServer` with `start()`/`stop()` and a
      per-challenge slot under the lock; idle page with auto-refresh; POST while
      idle handled without state change; clear error for a concurrent challenge.
      Reimplement `serve_captcha` as attach-or-start on a process-wide listener
      keyed by `(host, port)`, preserving its signature, the `CaptchaAnswer`
      return, the stderr announcement and `CaptchaTimeoutError.url`, and the
      `RuntimeError` message shape when a foreign process holds the port. Add
      `stop_shared_captcha_server()` with its `atexit` registration. Update the
      lifecycle tests that assert per-call port release, and add coverage for
      the idle page, the idle POST, PNG 404 while idle, two sequential
      challenges reusing the same port and URL, the concurrent-challenge error,
      and `stop()` releasing the port.
- [ ] 2. Wire `server.py`: start the listener with the session via
      `MCPServer(lifespan=...)`, stop it on exit, expose the form URL from
      `estado_servidor`, and keep stdout clean and `secrets` out of the file.
- [ ] 3. Wire `cli.py`: same shared-listener semantics for the one-shot path,
      releasing the port when the process ends.
- [ ] 4. Documentation: README flow, the idle page, the "nothing answers"
      meaning, and the accepted always-open-during-session surface. Record the
      verification evidence in this file.

## Evidence

Task 1 — commit `feat: keep the captcha form listener alive for the whole session`.

- `uv run pytest` → `109 passed, 1 skipped`. `uv run pytest tests/test_captcha.py`
  → `52 passed` (was 104 tests before this task; net +6).
- Independent verification by a separate read-only agent tried to falsify 12
  runtime claims (reuse without rebinding, idle page and its refresh directive,
  idle PNG 404, stale POST, `CaptchaBusyError` on concurrency, timeout freeing
  the slot, `stop()` releasing the port, the foreign-occupancy `RuntimeError`
  message, the untouched caller contract, no import-time binding, the token
  conflict, and the full suite). Result: 12/12 confirmed, no falsification.
- That verification found four defects, all fixed in the same task:
  1. A stale POST was accepted as the answer to the *next* challenge: a
     challenge that had expired left the human's tab showing an older image,
     and the submitted answer was then delivered to the live challenge. Since
     `fetch_full_text` allows only three attempts, a slow human could burn all
     three without understanding why. Answers are now bound to a per-challenge
     monotonic id carried in a hidden form field; a missing or mismatched id is
     discarded without touching any state.
  2. `stop()` returned early when the server had never been started, leaving the
     socket bound in `__init__` LISTENING. It now always closes the socket.
  3. The form URL was announced on stderr *before* the challenge was registered,
     so a call that then failed had already printed a URL for nothing. The
     announcement now runs from a callback invoked right after registration.
  4. A bare `except Exception: pass` around `server_close()` could hide exactly
     the leak in defect 2. It now reports on stderr.
- One further race was closed during review of those fixes: between the
  challenge-id check and `finish()`, the challenge can expire, and the handler
  previously sent the acknowledgement page regardless. The handler now uses
  `finish()`'s return value, so an answer that nobody received is reported as
  discarded instead of as received.

## Notes

- Design constraint discovered by reading the code, not assumed: the answer
  travels back to the blocked caller through a `threading.Event` plus a plain
  attribute, and `finish()`/`_shutdown_on_timeout()` latch `_finished` so the
  first answer always wins. That latch must move from the server to the
  challenge.
- `fetch_full_text` can call the captcha helper up to three times per document
  (`max_captcha_attempts=3`). Under the current design each attempt rebinds the
  same fixed port; under the new design each attempt registers a new challenge
  on the same listener.
