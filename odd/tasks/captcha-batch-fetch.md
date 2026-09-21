# Feature: captcha-batch-fetch

Make full-text fetching survive a batch: a non-blocking job queue over the
existing blocking fetch, so a research run over dozens of resolutions does not
hold an MCP call open for the whole batch.

## Intent

`ver_texto_completo` blocks. If CENDOJ answers with its `Control Descargas
masivas` captcha, the call waits for a human to type the answer, bounded by
`espera_segundos` (120 by default, against this deployment's 330 s client
request timeout). That is correct for one document and wrong for sixty: there
is no way to ask for a batch, and a second concurrent fetch collides with the
single challenge slot.

The reconnaissance for this feature found that the collision is not
theoretical. Sync MCP tool bodies run through `anyio.to_thread.run_sync`
(`.venv/lib/python3.12/site-packages/mcp/server/mcpserver/utilities/func_metadata.py:163`,
"A sync function runs on a worker thread"), so two `ver_texto_completo` calls
already execute on two threads today.

Two hazards were found that block any background fetching, and both are live
bugs independent of this feature:

1. `server.py:435` wraps the fetch in `contextlib.redirect_stdout(sys.stderr)`.
   That swaps the **process-global** `sys.stdout`. A fetch running on a
   background thread prints outside that block, so its `print` in
   `cendoj.py:832-835` reaches real stdout and corrupts the stdio JSON-RPC
   stream. A background job therefore cannot be added before this is fixed.
2. `CendojClient._ensure_session` (`cendoj.py:617-623`) guards a plain bool with
   no lock, so two threads entering cold both issue the `JSESSIONID` bootstrap
   GET. The single-bootstrap invariant is asserted only for sequential calls
   (`tests/test_server.py:538`).

A third finding shapes the design rather than blocking it: the captcha is
**session-sticky**. One shared `httpx.Client` means one cookie jar and one
CENDOJ session, and per `README.md:196-199` a solved challenge usually stops
later full-text requests from asking again. So a batch does not need many
concurrent challenges; it needs **ordering** and a way to hand the human one
challenge at a time.

## Scope

In scope:

- Make the client safe to drive from more than one thread: lock the session
  bootstrap, and stop relying on a process-global stdout redirect by routing
  the fetch's progress line to stderr.
- A job registry with a bounded worker pool (default concurrency 1) that runs
  the existing blocking `fetch_full_text` unchanged on a worker thread.
- Non-blocking tool surface to start jobs and collect their results, additive
  to `ver_texto_completo`, which keeps its blocking contract.
- Make the post-answer page auto-refresh, so a queued batch does not require a
  manual reload between challenges.

Out of scope, deliberately:

- Any change to `0.0.0.0`/`::`/empty host rejection, token validation, the token
  required on every path, or the wrong-token 404. Frozen by
  `captcha-persistent-listener`.
- More than one captcha challenge pending at a time. The single slot and the
  per-challenge id binding stay exactly as they are: an answer must still be
  bound to one challenge and never delivered to a later one.
- Any automatic captcha solver, OCR, vision model or third-party service.
- Any rate-limit claim. The reconnaissance found **no** numeric quota, no
  `Retry-After` handling and no backoff anywhere in the repository, and CENDOJ's
  mass-download gate is detected only by two Spanish substrings
  (`cendoj.py:554`). The concurrency limit is therefore a product decision and
  must be documented as one, never presented as a site-published limit.
- Rewriting `fetch_full_text` as a resumable state machine. The queue runs the
  existing blocking function on a thread; that is the whole trick.

## Constraints

- stdout stays pure JSON-RPC and every human-facing message stays on stderr.
  Nine tests assert `capsys.readouterr().out == ""`; they are the guard rail for
  the stdout fix and must keep passing.
- `cendoj.py`'s progress line must remain visible somewhere: it is the only
  trace that a fetch started.
- The listener keeps one instance per `(host, port)` key and is owned by the
  session lifespan. The queue must never bind or release a socket.
- `serve_captcha` keeps its signature, its `CaptchaAnswer` return and its
  stderr announcement fired only after registration: thirteen tests poll stderr
  for `http://` as their readiness signal (`tests/test_captcha.py:52`).
- `tests/test_server.py:720` pins the exact kwargs `server.py` passes to
  `fetch_full_text`, and `tests/test_documents.py:117` pins `serve_captcha`'s
  call shape from inside `fetch_full_text`. The job worker must reuse those
  call sites rather than introduce a third shape.
- A job's result payload reuses `ver_texto_completo`'s shape, so a caller can
  treat a collected job and a direct fetch identically.
- Deterministic tests only; the `live` marker stays deselected by default.
- No new dependency.

## Design summary

- `cendoj.py` first: the bootstrap becomes lock-guarded, and the progress line
  moves to stderr so any thread can call the fetch safely.
- A new module owns the jobs: a registry keyed by a job id, a bounded worker
  (default one) consuming a FIFO queue, and per-job state plus the final
  payload. The worker calls the existing `fetch_full_text`; nothing in the
  captcha layer changes.
- `server.py` gains the non-blocking tools and drops the now-unnecessary stdout
  redirect. `ver_texto_completo` keeps its current behaviour and payload.
- `captcha.py` gains the refresh on the post-answer page only; the form page
  deliberately keeps no refresh, because a changing image mid-typing is worse
  than a stale page, and the stale page already self-heals by refreshing.

### API surface (decided)

Three tools, one responsibility each, with a deliberate split between metadata
and payload. `ver_texto_completo` keeps its blocking contract untouched.

- `iniciar_descargas(urls)` -> `{captcha_url, max_concurrentes, jobs:
  [{job_id, url, state}]}`. The worker count comes from the
  `NAVAJA_MAX_CONCURRENTES` environment variable (default 1), matching the
  repository's existing env-var convention for captcha and PDF configuration,
  rather than from a per-call parameter: once the queue is a process singleton a
  per-call value is ambiguous, and silently ignoring a later differing request
  would be dishonest, while refusing it would be needless friction.
- `estado_descargas()` -> metadata for every job and never the text:
  `[{job_id, url, state, attempts, pdf_path, error_code}]`. This is what makes
  polling a sixty-document batch cheap; returning full payloads here would be
  unusable at that size.
- `recoger_descarga(job_id)` -> the complete payload for one job, in exactly
  `ver_texto_completo`'s shape, so a caller treats a collected job and a direct
  fetch identically.

`max_concurrentes` defaults to 1. The captcha is session-sticky, so one solved
challenge normally serves a whole batch. That default is a product decision of
this project, not a site-published limit: the reconnaissance found no quota, no
`Retry-After` and no backoff anywhere in the repository, so it must never be
documented as a CENDOJ rule.

Job state values are English tokens (`queued`, `running`, `done`, `failed`),
matching the existing payload vocabulary (`error_code` values, `pdf_save_reason`
values) rather than the Spanish tool and parameter names.

## Tasks

- [x] 1. `cendoj.py`: lock the session bootstrap and route the progress line to
      stderr; update any test that asserted the stdout print. Add a
      deterministic two-thread test for the single bootstrap.
- [x] 2. New job queue module with the registry, bounded worker and per-job
      state, plus its tests.
- [x] 3. `server.py`: non-blocking tools, the collected payload shape, and the
      removal of the stdout redirect.
- [x] 4. `captcha.py`: auto-refresh on the post-answer page.
- [x] 5. Documentation: README, tool descriptions, and this file.

## Evidence

Baseline before this feature: `.venv/bin/python -m pytest` -> `283 passed,
1 skipped`, on branch `feat/cendoj-search`, clean tree at `56abdca`.

### Work unit 1 — `src/navaja/cendoj.py` + `tests/test_documents.py`

The two hazards that block background fetching are fixed. `_ensure_session` now
guards the bootstrap with a per-instance `threading.Lock` and double-checked
locking, and the fetch's progress line goes to stderr with `flush=True` instead
of to stdout, so no thread depends on the process-global stdout redirect any
more.

- New tests, red then green: the progress line lands on stderr with empty
  stdout, and two cold threads issue exactly one bootstrap GET. Red evidence:
  `captured.out` still held the line, and the bootstrap count was 2 where 1 was
  expected.
- Full suite: `283 passed, 1 skipped` -> `285 passed, 1 skipped`.
- Checked by the parent against a temporarily reverted `src/navaja/cendoj.py`,
  with the time bound on the pytest invocation rather than on the whole shell
  command: the concurrency test fails in 2 s with exit 1 and the process exits
  on its own. The restored file's sha256 was compared against the
  pre-experiment value.

One defect was found and fixed inside this work unit. The first version of the
concurrency test used non-daemon threads parked on a `threading.Event`; when the
assertion failed the event was never set, and interpreter shutdown joined the
threads forever, so the test **hung instead of failing**. Observed cost: a 300 s
hang that also prevented a `cp` restore from running in the same shell command,
leaving a half-mutated tree. The test now uses `daemon=True` and releases the
event, joins and closes the client from a `finally`. Measured delta: pre-fix the
same assertion was killed by `timeout 30` at about 30.1 s (exit 124); post-fix it
fails in about 0.9 s and exits with status 1.

### Work units 2 to 5

- Task 2, `src/navaja/jobs.py` + `tests/test_jobs.py` (new): a CENDOJ-agnostic
  bounded FIFO queue over an injected blocking `runner(url) -> dict`, with
  `JobState`, `submit`/`submit_many`, metadata-only `estado()`, `recoger()`, and
  a bounded idempotent `stop()`. Six review defects were found and fixed in a
  follow-up round: a docstring that falsely claimed the failure payload matched
  `ver_texto_completo` (a layering error, not just a doc error, since this module
  must not know the tool's shape), module-level mutable markers shared across
  callers, an over-broad `except BaseException`, unsynchronised state reads,
  undocumented `stop()` semantics for queued jobs, and lowercase `StrEnum`
  members.
- Task 3, `src/navaja/server.py` + `tests/test_server.py`: `ver_texto_completo`'s
  body was extracted into one shared helper used by both the blocking tool and
  the job runner, so no second call shape exists; the process-global
  `contextlib.redirect_stdout` was removed as redundant; the `JobQueue` singleton
  is created lazily under a lock with its worker count from
  `NAVAJA_MAX_CONCURRENTES`; and the three batch tools were added with payload
  normalisation for a crashed runner.
- Task 4, `src/navaja/captcha.py`: the post-answer page now auto-refreshes and no
  longer tells the human to close the tab unconditionally, so a queued batch does
  not strand them.
- Task 5: README batch section, the three tool descriptions, and the state
  semantics note.
- Suite progression: `283 passed, 1 skipped` before the feature, `285` after task
  1, `308` after task 2, `322` after task 3, `324 passed, 1 skipped` after tasks 4
  and 5.

Independent verification of the core (tasks 1 to 3) confirmed five claims against
real entry points, including a real stdio JSON-RPC handshake with the batch tools
and a byte-identical comparison of `ver_texto_completo`'s signature and its single
`fetch_full_text` call against `HEAD`. It found four items: the undocumented state
semantics (now documented), the private-attribute access (now a public
`max_concurrentes` property), the two remaining tasks (now done), and one
pre-existing defect recorded below.

### Pre-existing defects surfaced but deliberately NOT fixed here

1. `CaptchaServer.stop()` is unbounded. With a client connected but silent, the
   handler blocks inside the serving thread and `BaseServer.shutdown()`, which
   takes no timeout, blocks `stop()` indefinitely: the `thread.join(timeout=5.0)`
   is never reached. Observed still blocked after 3 s. This file's stop semantics
   are frozen by `captcha-persistent-listener`, and changing them needs its own
   risk budget, so this feature leaves it alone.
2. `tests/test_captcha.py::test_timeout_keeps_listener_bound_on_repeated_timeouts`
   flaked once in six full-suite runs with `port ... still bound after
   stop_shared_captcha_server()`. Verification established it as pre-existing,
   not caused by this feature: both `tests/test_captcha.py` and
   `src/navaja/captcha.py` are byte-identical to the baseline, pytest collects
   `test_captcha.py` first, nothing in the new code opens a socket, the exact
   scenario repeated 150 times in-process produced zero anomalies, and no
   `Captcha server stop warning:` line appeared in the failing run, so
   `shutdown()` and `server_close()` both succeeded. `_port_is_free` is a trial
   `bind()` without `SO_REUSEADDR`, so it also reports a `TIME_WAIT` socket as
   occupied. The exact trigger remains unproven.

### Known deviation, flagged but not reverted

Task 4 also changed the post-answer page's title from `Recibido` to
`CENDOJ captcha`, which the task did not ask for. It is benign: that page is only
reachable by POST, while `cli.py`'s listener marker is read from a GET, so no
behaviour depends on it. Left in place rather than spending a round on one line,
but recorded so a reviewer can weigh it.
