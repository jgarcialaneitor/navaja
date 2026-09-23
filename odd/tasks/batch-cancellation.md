# Feature: batch cancellation (#19, Proposal 3)

GitHub issue: https://github.com/jgarcialaneitor/navaja/issues/19
Branch: `feat/batch-cancellation` (off `main` `15ccb78`)

## Problem (measured in #17)

`iniciar_descargas` with N URLs in an instance whose listener could not serve
the challenge: several jobs returned `captcha_busy`, then a job sat in
`running` waiting for the captcha. There is no tool to cancel the batch, so a
stuck batch costs up to N × `captcha_timeout` (300 s each by default), and
`estado_servidor` kept reporting `captcha_listening: true` the whole time.

## Decisions taken (user, this session)

1. **Scope**: Proposal 3 only (batch cancellation). Proposal 4 (release the
   abandoned challenge when the MCP client disconnects) stays deferred with
   its own design round — an MCP client may legitimately disconnect and
   reconnect, so it needs better evidence than a socket close.
2. **Mid-fetch semantics**: the RUNNING job is allowed to finish; only QUEUED
   jobs are cancelled. No thread interruption.

## Design

- New `JobState.CANCELLED` in `src/navaja/jobs.py`.
- `JobQueue.cancel_batch(batch_id) -> dict`:
  - Unknown batch id → `{"ok": False, "error_code": "unknown_batch", ...}`
    (no exception; same honesty style as `recoger`).
  - QUEUED jobs → `CANCELLED` with a payload `{ok: False, error_code:
    "cancelled", ...}`; `finished.set()` so nothing blocks on them.
  - Race guard: `_run_job` re-checks state under the lock before running and
    skips jobs already `CANCELLED`.
  - DONE/FAILED jobs untouched; RUNNING job untouched (finishes normally).
  - Idempotent: cancelling twice reports the same final states, no errors.
  - Return a per-batch summary: `{"ok": True, "batch_id", "cancelled",
    "already_finished", "left_running"}`.
- New server tool `cancelar_lote(batch_id) -> dict` in `src/navaja/server.py`,
  Spanish naming consistent with `iniciar_descargas` / `estado_lote` /
  `recoger_resultados`. Docstring states the semantics (queued → cancelled,
  running → finishes) and that a cancelled job's `recoger` returns
  `error_code: "cancelled"` (additive to the published reason vocabulary).
- `estado_lote` needs no shape change: it already surfaces `state` and
  `error_code` per job; `cancelled` flows through naturally.

## Tasks

- [ ] 1. Branch + this doc
- [x] 2. `JobState.CANCELLED` + `JobQueue.cancel_batch` with race guard (TDD) — commit `325571e`
- [x] 3. `cancelar_lote` server tool + recoger/estado_lote integration (TDD) — commit `325571e`
- [x] 4. README: tool row + cancelled vocabulary — commit `3fca2d1`
- [ ] 5. Full suite, work-unit commits, PR
