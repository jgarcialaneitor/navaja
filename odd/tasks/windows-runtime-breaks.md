# Windows runtime breaks

Follow-up to `windows-import-safety.md`. That work made the package importable and
the suite clean of its POSIX-only assumptions. The first real `windows-latest` CI
run then measured what the static audit had only guessed at, and it found more —
including one break in **shipped code**, in the same file issue #8 was about.

Baseline: `348 passed, 2 skipped` on Linux; Windows CI on PR #9 measured
`46 failed, 299 passed, 4 skipped, 2 errors in 59.32s`.

## Problems being fixed

1. **`os.fchmod` is POSIX-only and absent on Windows** (`captcha.py:186`, inside
   `_persist_token`). The static audit had this exact line in hand and filed it
   under "import-safe but the permission semantics silently no-op", which was
   wrong: the attribute does not exist, so the call raises `AttributeError` and
   the captcha token is never persisted. 38 direct failures, plus 16
   `test_server.py` failures downstream because the captcha form cannot start.
   This is the same defect class as the `fcntl` import in issue #8.
2. **A parametrized test id blows pytest's own environment limit**
   (`tests/test_cendoj_client.py`, `test_detect_refusal_classifies_response`).
   The parameters are whole HTML documents, so pytest sets `PYTEST_CURRENT_TEST`
   to a string longer than Windows' 32767-character limit and its own
   `pytest_runtest_setup` / `pytest_runtest_teardown` hooks raise `ValueError`.
   Reported as 2 errors, in `setup` and `teardown`, not as test failures.
3. **A refused bind is not reported as address-in-use on Windows.** `serve_captcha`
   maps `errno.EADDRINUSE` to a friendly `RuntimeError`; on Windows the same
   situation surfaces as `PermissionError` (`WSAEACCES`, winerror 10013), so the
   friendly message never appears and the raw `OSError` escapes.
   `test_bind_to_occupied_port_raises_clear_error` fails on that.

## Decided semantics

- The `0600` guarantee on the token file is **POSIX-only** and the docstring must
  say so. On Windows the fallback is `os.chmod`, which exists and only toggles the
  read-only bit; that is best-effort, not an equivalent guarantee. Do not claim
  otherwise in prose.
- The Windows bind refusal must be detected **precisely**, by the Windows error
  code. `EACCES` alone must never be mapped to "address in use", because on POSIX
  that errno also means "privileged port, and you are not root", which is a
  different problem with a different remedy.
- The `windows-latest` CI job stays non-blocking. It is the instrument; it is not
  flipped to a gate in this work unit.

## Tasks

- [x] 1. Keep `_persist_token` working when `os.fchmod` is absent, falling back to
      `os.chmod` on the temporary path, and state the POSIX-only nature of the
      `0600` guarantee in the docstring.
- [x] 2. Test the missing-`os.fchmod` path the way the `fcntl` guard is tested: by
      removing the attribute, not by stubbing the call site.
- [x] 3. Give the HTML-parametrized test short explicit ids so pytest's own
      environment variable stays within the Windows limit, without changing what
      the test asserts.
- [x] 4. Report a refused bind as address-in-use on Windows by matching the Windows
      error code, with a test that simulates it and without mislabeling POSIX
      `EACCES`.
- [x] 5. Verify on Linux that nothing regressed, then push so the Windows job
      measures the result.

## Evidence

Suite: `348 passed, 2 skipped` -> `350 passed, 2 skipped`. The skip set is
unchanged: `-rs` reports only the two opt-in `live` tests.

- Mutation A: removing the `hasattr(os, "fchmod")` branch makes
  `test_resolve_token_persists_when_fchmod_unavailable` fail with
  `AttributeError: module 'os' has no attribute 'fchmod'`.
- Mutation B: narrowing the bind condition back to `errno.EADDRINUSE` makes
  `test_bind_refusal_on_windows_raises_clear_error` fail with the raw `OSError`.
- Mutation C: the real POSIX occupied-port test still passes, and a POSIX
  `OSError` carrying only `errno == EACCES` is **not** mapped to "address already
  in use" — `getattr(exc, "winerror", None)` is `None` on POSIX, so the compound
  condition stays false and the original error is re-raised.
- The parametrized ids did not drop a case: four cases collected, four ids, all
  passing; the parameter list itself is untouched.
- `documents.py` was switched to the same explicit `hasattr` capability check.
  That closes the `R2-overbroad-attributeerror` finding the review raised, and
  leaves one idiom for "this POSIX-only `os` attribute may not exist" instead of
  two.

Deliberately **not** verified here: anything about Windows. No Windows host
exists in this environment, so every Windows-facing claim in this work unit is
structural. The `windows-latest` CI job is what measures it, and it stays
non-blocking until it is green.
