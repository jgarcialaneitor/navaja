# Windows import safety

Issue #8: `captcha.py` imports `fcntl` unconditionally, which is POSIX-only, so
the whole package fails to import on Windows. It is an import-time failure, not a
call-time one: `navaja/__init__.py` and `cendoj.py` both import the module, so
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
- Making the *test suite* run on Windows, and adding a Windows CI runner, are a
  separate follow-up work unit below (tasks 5-8), not part of the package fix.

## Follow-up work unit: make the suite Windows-clean (tasks 5-8)

Tasks 1-4 made the *package* import-safe on Windows and deliberately left the
*test suite* alone. A read-only audit of every Unix-only assumption in `src/` and
`tests/` found **0 collection/import breaks** (tasks 1-4 closed those) but **8
deterministic test failures across 6 tests**, all in two files, plus 12
conditional socket/timing risks that only a real Windows run can settle.

| Site | Failure on Windows |
| --- | --- |
| `tests/test_documents.py:587` | `monkeypatch.delattr(os, "pathconf")` raises when the attribute is already absent, so the test written to simulate Windows cannot run on Windows. |
| `tests/test_documents.py:604` | bare `os.pathconf(tmp_path, "PC_NAME_MAX")`; the `pytest.skip` below it is unreachable because `AttributeError` escapes first. |
| `tests/test_documents.py:643` | `os.geteuid()` is POSIX-only, so the root guard errors out. |
| `tests/test_documents.py:650` | `chmod(0o000)` does not make a file unreadable on Windows, so the collision branch is never reached. Masked today by the `geteuid` error above. |
| `tests/test_documents.py:626` | expects `IsADirectoryError`/`EISDIR`; Windows raises `PermissionError` instead. |
| `tests/test_documents.py:426` | sets `HOME`, which `Path.home()` ignores on Windows (`ntpath.expanduser` reads `USERPROFILE`). |
| `tests/test_captcha.py:791` | asserts mode `0o600`; Windows reports regular files as `0o666`. |
| `tests/test_captcha.py:792` | asserts mode `0o700`; `mkdir(mode=...)` is ignored on Windows. |

Rules for this work unit:

- **No test that passes on Linux today may end up skipped on Linux.** Windows-clean
  must not cost Linux coverage.
- Prefer a platform-neutral simulation consistent with each file's existing
  monkeypatch style over a platform skip. Skip only where the property under test
  is genuinely POSIX-specific (file modes are), and then with an exact reason.
- **Do not change `src/` behaviour to make a test pass.** The Windows-specific
  gaps in `documents.py` (directory detection via `EISDIR`, reserved device names
  such as `NUL`/`CON`, byte-vs-character name length) are product decisions and stay
  recorded here, not smuggled into a test-only change.

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
- [x] 5. Make `tests/test_documents.py` Windows-clean: the six sites above.
- [x] 6. Make `tests/test_captcha.py` Windows-clean: the two mode assertions.
- [x] 7. Add a `windows-latest` CI job so the fix is measured instead of asserted.
- [x] 8. Record the Windows residuals (the 12 conditional risks and the
      `documents.py` product gaps) so the next reader does not rediscover them.

## Follow-ups, not fixed here

- The 12 conditional Windows risks are almost all socket/bind/timing: whether
  `socket.if_nameindex()` behaves on the Windows build, whether a bind failure maps
  to `EADDRINUSE`, whether `SO_REUSEADDR` lets `HTTPServer` take an occupied port,
  and whether the 2.0 s URL-announce budget survives a shared runner. Only a real
  Windows run answers these.
- `src/navaja/documents.py`: `_sanitize_filename` accepts Windows reserved device
  names (`CON`, `NUL`, `COM1`…) and trailing dots/spaces; `len(os.fsencode(name))`
  measures bytes where Windows limits characters. No test covers either.
- Issue #7's body points at `tests/test_documents.py` for
  `test_url_validator_error_message_names_every_accepted_collection`; the test
  actually lives in `tests/test_cendoj_client.py:424`.

## Evidence

### Tasks 1-4

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
- Full suite at the end of tasks 1-4: `347 passed, 2 skipped` (baseline before the
  work: `343 passed, 2 skipped`).

### Tasks 5-8

Suite: `347 passed, 2 skipped` -> `348 passed, 2 skipped` (the +1 is the new
POSIX-permission test). The skip set is unchanged: `-rs` reports exactly the two
opt-in `live` tests, so no previously-passing Linux test became skipped.

Per-site disposition:

| Site | Mechanism |
| --- | --- |
| `test_documents.py` pathconf delattr | simulation: only delete the attribute when it exists, so the Windows simulation runs on Windows too. |
| `test_documents.py` name-limit probe | simulation: catch `AttributeError` and fall back to `255`, so the overlong-name path is exercised on Windows instead of erroring. |
| `test_documents.py` root guard | simulation: consult `os.geteuid` only when the platform has it. |
| `test_documents.py` unreadable target | simulation: synthetic `PermissionError` from `read_bytes`, the pattern this file already uses for its `surfaces_*` tests. Assertions unchanged. |
| `test_documents.py` unreadable target, real permissions | new POSIX-only test, kept additive, so the real on-disk `chmod(0o000)` scenario is still covered on the platform that can test it. Skipped on Windows with an exact reason. |
| `test_documents.py` directory collision | skip on Windows only: the platform reports `PermissionError` instead of `EISDIR`, and mapping it to `directory_collision` would be a `src/` behaviour change, which is out of scope. |
| `test_documents.py` `HOME` | simulation: set the variables the running platform's `expanduser` actually reads (`USERPROFILE`, and clear `HOMEDRIVE`/`HOMEPATH`, on Windows). |
| `test_captcha.py` modes `0o600`/`0o700` | guarded by `if os.name != "nt"`: file modes are genuinely POSIX-specific. |

Independent verification, with evidence:

- The `except OSError` around `_unique_path` in `documents.py` is load-bearing:
  removing it makes **both** unreadable-collision tests fail with
  `PermissionError: [Errno 13] Permission denied`; restoring it returns the file to
  sha256 `c09617f3…` and both pass again.
- The mode assertions still run on Linux: forcing `0o600` -> `0o644` in
  `test_captcha.py` makes `test_resolve_token_generates_and_persists` fail, so the
  `os.name != "nt"` guard does not silently skip them here.
- `.github/workflows/ci.yml` parses (PyYAML 6.0.3); the Ubuntu job is byte-identical
  apart from the appended job; `continue-on-error: true` appears only on the Windows
  job; `NAVAJA_LIVE` appears nowhere.
- `git diff src/` is empty: no shipped behaviour changed for a test to pass.
- `README.md` test counts updated to the observed `348 passed, 2 skipped`.

Incident during verification, recorded because it can recur: the verifier cleaned up
a mutation with `git checkout -- tests/test_captcha.py`, which restored the **HEAD**
version and destroyed the candidate's uncommitted edit. It recovered the exact bytes
from git blob `de16b43868b11485f29499cc4945412dce2e1ff0` and the file's sha256 was
re-confirmed as `3e780f2f852504bb0719573d91a118667b18d91cab3f70858342607a76944ee9`.
Lesson: while a candidate is uncommitted, restore mutated files from a byte backup,
never from `git checkout`.
