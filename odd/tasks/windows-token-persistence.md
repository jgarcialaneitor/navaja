# Windows token persistence

Follow-up to `windows-runtime-breaks.md`. That work took the Windows CI from
`46 failed, 2 errors` down to `2 failed, 346 passed, 4 skipped`. Both remaining
failures trace to a single call.

Baseline: `350 passed, 2 skipped` on Linux; Windows CI on PR #10 measured
`2 failed, 346 passed, 4 skipped in 62.48s`.

## Problem being fixed

`_persist_token` writes the token to a temporary file and moves it into place
with `os.replace` so a concurrent run cannot observe a torn file. On Windows that
call raises:

```
PermissionError: [WinError 5] Access is denied:
  '…\\navaja\\.captcha-token-hds3lb4p' -> '…\\navaja\\captcha-token'
```

Both remaining Windows failures come from it:
`test_batch_multi_url_completes` fails on the exception itself, and
`test_iniciar_descargas_rejected_call_does_not_affect_later_batch` fails with
`valid batch did not finish`, a timeout waiting for jobs that never start
because the captcha token was never persisted.

POSIX allows renaming over a file that is open; Windows does not. The usual
cause is a brief hold on a just-written file — a real-time virus scanner on a CI
runner is the classic one — which is exactly the shape of a fresh temp file in a
fresh directory.

**This is not a regression from the previous work unit.** Before it, the function
died at `os.fchmod` and never reached `os.replace`; fixing that exposed this.

## Decided semantics

- Keep `os.replace` and the atomicity guarantee. Do **not** degrade to writing the
  final path directly: a torn token file is worse than a retry, and the guarantee
  is documented.
- Retry a small, bounded number of times with a short backoff, and only for the
  failure Windows actually produces. After the attempts are exhausted, re-raise
  the original error, so a genuine permission problem still surfaces instead of
  being masked.
- Do not gate the retry on the platform. On POSIX it will never trigger (renaming
  over an open file works), and a conditional would add a branch that cannot be
  exercised there.
- Clean up the temporary file when the move ultimately fails. Today a failed
  `os.replace` leaves `.captcha-token-*` litter behind, because the existing
  cleanup only covers the write stage.

## Tasks

- [x] 1. Retry `os.replace` a bounded number of times with a short backoff in
      `_persist_token`, re-raising after the last attempt, and clean up the
      temporary file on final failure.
- [x] 2. Test the transient case: a move that fails once and then succeeds must
      still persist the token, asserted against the full value.
- [x] 3. Test the persistent case: a move that always fails must raise, and must
      leave no temporary file behind.
- [x] 4. Name the two Windows bind error codes as named constants instead of
      leaving bare integers, keeping a single "address already in use" reason
      text because the Windows CI measured that 10013 means an occupied address
      at this HTTPServer/SO_REUSEADDR call site.
- [x] 5. Verify on Linux, then push so the Windows job measures it.

## Evidence

Suite: `350 passed, 2 skipped` -> `353 passed, 2 skipped`. The skip set is
unchanged: `-rs` reports only the two opt-in `live` tests.

- **The retry is load-bearing**: setting `_TOKEN_REPLACE_MAX_ATTEMPTS = 1` makes
  `test_persist_token_retries_transient_move_failure` fail on the first transient
  `PermissionError`.
- **Cleanup covers every ultimate failure**, not only exhausted retries: a
  throwaway check raising a non-retryable `OSError(errno.ENOSPC)` showed the error
  propagating unchanged on attempt 1 (call count 1) with no `.captcha-token-*`
  litter left in the directory.
- **The tests assert what they claim**: the transient test proves the retry ran by
  counting calls and compares the full token value; both litter checks enumerate
  the directory rather than guessing a filename.
- **The reason text is now pinned**: the bind test is parametrized over
  `10013`/`10048` with the full message anchored `^…$`. Before this, the
  assertion matched only the generic prefix and would have passed against either
  reason.
- **The Windows bind message stayed unified**: both error codes produce the
  same "address already in use" reason. The attempted split was reverted after
  the Windows CI measured the call-site behaviour:
  1. The bind message was split by error code so that `WSAEACCES` reported
     "access to the requested address was denied".
  2. The Windows CI then failed
     `test_lifespan_does_not_crash_on_occupied_port`, whose assertion at
     `tests/test_server.py:1076` requires the stderr to contain
     `address already in use`. On Windows an occupied address arrives as
     `WSAEACCES`, because `HTTPServer` binds with `SO_REUSEADDR`.
  3. The split was reverted: both codes produce one reason text again, and the
     reason 10013 belongs there is recorded as a comment in the source rather
     than as a hedged message. The named constants were kept.
  4. The finding that suggested splitting the message was right about the error
     code in general and wrong about this call site; the measurement decided it.

Deliberately **not** verified here: anything about Windows. The CI job on the
pull request is the measurement, and it is the only thing that can say whether a
bounded retry is actually enough for the `os.replace` refusal.
