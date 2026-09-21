# Batch registry hardening

Follow-up to `captcha-batch-fetch.md`. That feature shipped working batch tools,
but the job registry is unbounded and unscoped, which breaks the second batch of
a session and grows memory without limit.

Baseline before this work: `.venv/bin/python -m pytest` -> `324 passed, 1 skipped`,
on branch `feat/cendoj-search`, clean tree at `d882b53`.

## Problems being fixed

1. `JobQueue.estado()` returns every job for the process lifetime, so polling a
   second batch also re-returns the first one.
2. Finished payloads (full document text) are retained forever.
3. `iniciar_descargas` accepts an unbounded URL list and validates nothing, so a
   typo only surfaces once a worker picks the job up.
4. `_worker_loop` polls `self._work.get(timeout=0.1)`, waking every worker ten
   times a second forever, and its `except Exception: continue` swallows more
   than `queue.Empty`.
5. `_run_fetch_job` hardcodes `espera_segundos=300`, silently duplicating the
   captcha wait default.

## Decided semantics (user decision, do not revisit)

- Batch scoping: `iniciar_descargas` returns a `batch_id`. `estado_descargas`
  takes an optional `batch_id` and defaults to the most recent batch.
- `recoger_descarga` stays non-destructive: calling it twice returns the same
  payload.
- Memory is bounded by pruning whole old batches, keeping the most recent ones.
  A batch with queued or running jobs is never pruned.

## Tasks

- [x] 1. Add batch identity to `JobQueue`: `submit_many` returns a `batch_id`,
      each `Job` records it, `estado(batch_id=None)` defaults to the newest batch
      and accepts an explicit one. Unknown `batch_id` returns an empty list, not
      an error.
- [x] 2. Bound the registry: retain the most recent `_MAX_BATCHES_RETENIDOS = 10`
      batches, pruning oldest-first and never pruning a batch that still has
      queued or running jobs. Pruned job ids then read as `unknown_job`.
- [x] 3. Replace the 0.1 s polling loop with a real blocking `get()` driven by
      the existing sentinel, and narrow `except Exception` to `queue.Empty`.
- [x] 4. Cap and validate input in `iniciar_descargas`: reject more than 100 URLs
      with `error_code: "too_many_urls"`, and reject unparseable URLs up front
      with `error_code: "invalid_url"` reusing `parse_document_url`, without
      enqueuing anything from a rejected call.
- [x] 5. Remove the hardcoded `300` from `_run_fetch_job` in favour of the shared
      captcha wait default.
- [x] 6. Update `README.md` (batch section, tool list, config table) and record
      the measured suite count.

## Evidence

- Measured suite result: `338 passed, 1 skipped`.
- `test_busy_batch_is_never_pruned` originally took ~50 s because the shared
  gated runner blocked all ten terminal batches; it was fixed to gate only the
  "busy" URL so the older terminal batches can finish and be pruned.
