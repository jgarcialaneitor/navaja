# Windows import safety

Issue #8: `captcha.py` imports `fcntl` unconditionally, which is POSIX-only, so
the whole package fails to import on Windows. It is an import-time failure, not
a call-time one: `navaja/__init__.py` and `cendoj.py` both import the module, so
neither `navaja-doc` nor `navaja-mcp` starts.

Baseline before this work: `.venv/bin/pytest` -> `343 passed, 2 skipped` in
41.33s, clean tree at `2e30b0d` (`main`). Work happens on `fix/windows-import-safety`.

## Problems being fixed

1. `captcha.py:13` imports `fcntl` at module level. `fcntl` does not exist on
   Windows, so `import navaja` raises `ModuleNotFoundError` before any function
   runs.
2. `_interface_ipv4` (`captcha.py:64`) is the only consumer, and only for the
   `SIOCGIFADDR` ioctl. Its documented contract is already "non-Linux platforms
   return an empty list" (`_list_network_interfaces` docstring), which the code
   cannot honour today because it cannot even be imported.
3. Same defect class, found during reconnaissance and not covered by issue #8:
   `documents.py:388` calls `os.pathconf`, which is POSIX-only and absent on
   Windows. The surrounding `except (ValueError, OSError)` does not catch the
   resulting `AttributeError`, so any PDF save would crash.

## Decided semantics

- A missing `fcntl` degrades `tailscale0` auto-detection only. Resolution order
  stays env var -> `tailscale0` -> `127.0.0.1`; the terminal step is unchanged
  and already documented in the README.
- The fallback name limit stays `255` when `PC_NAME_MAX` cannot be queried, which
  is what the existing `except (ValueError, OSError)` branch already returns.
- Out of scope, deliberately: making the *whole test suite* run on Windows
  (`tests/test_documents.py:588` uses `os.pathconf`, `:627` uses `os.geteuid`),
  and adding a Windows CI runner. No Windows host is available to verify either,
  so they are recorded as follow-ups rather than claimed as fixed.

## Tasks

- [x] 1. Make the `fcntl` import optional in `captcha.py` and short-circuit
      `_interface_ipv4` when it is unavailable, so the module imports and
      `tailscale0` detection degrades instead of crashing.
- [x] 2. Add tests proving the package imports and `resolve_captcha_host` still
      resolves when `fcntl` is absent, by hiding it from the import system the way
      Windows would (not by stubbing the call site only).
- [x] 3. Guard the POSIX-only `os.pathconf` name-limit probe in `documents.py`,
      keeping `255` as the fallback, with a test that simulates its absence.
- [x] 4. Confirm the module docstrings state the degrade behaviour truthfully, and
      touch the README only if this change makes an existing claim wrong.

## Follow-ups, not fixed here

- The offline suite still cannot run on Windows: `tests/test_documents.py:588`
  queries `os.pathconf` and `:627` calls `os.geteuid()`, both POSIX-only.
- Nothing verifies the fix on a real Windows host. A Windows CI job would, even as
  a bare import smoke step, and is deliberately out of scope here.
- Issue #7's body points at `tests/test_documents.py` for
  `test_url_validator_error_message_names_every_accepted_collection`; the test
  actually lives in `tests/test_cendoj_client.py:424`.

## Evidence

- Red then green, observed: before the guard the subprocess test failed with
  `ModuleNotFoundError: No module named 'fcntl'` raised from `captcha.py:13`
  through `navaja/__init__.py:3`; the two unit tests failed with
  `AttributeError: 'NoneType' object has no attribute 'ioctl'`.
- The subprocess test blocks the name from `sys.meta_path`, so its result is
  decided by the guard and not by the host's real interfaces: this machine has a
  live `tailscale0`, and the test still resolves to `127.0.0.1` deterministically.
- Mutation check: restoring the module-level `import fcntl` makes exactly that
  subprocess test fail, so the guard is load-bearing. The two unit tests keep
  passing under that mutation, because they exercise the `fcntl is None`
  behaviour rather than the import guard itself.
- Task 3: red observed as `AttributeError: module 'os' has no attribute
  'pathconf'. Did you mean: 'fpathconf'?` at `documents.py:388`; mutation check
  confirms that narrowing the except clause back to `(ValueError, OSError)` makes
  the new test fail.
- Task 4: `_interface_ipv4`'s docstring now states the degrade path;
  `README.md` was not touched, because its resolution-order and fallback claims
  were already true and this change is what makes them true on Windows.
- Full suite at the end of the work: `347 passed, 2 skipped` (baseline before the
  work: `343 passed, 2 skipped`).
