"""Bounded FIFO job queue for blocking full-text fetches.

The queue is deliberately ignorant of CENDOJ, HTTP and the captcha layer: the
caller injects a blocking ``runner(url) -> dict``.  This keeps the module
offline-testable and lets the server layer reuse the existing
``fetch_full_text`` unchanged.

A successful job returns whatever payload the injected runner produced.  When
the runner raises, the queue synthesises a minimal, runner-agnostic failure
payload (``ok``, ``error_code``, ``error``, ``text``).  Normalising either
payload to the server tool's shape — adding the PDF keys expected by
``ver_texto_completo``, for example — is the caller's responsibility, not this
module's.

Public API:

* :class:`JobState` — ``queued`` | ``running`` | ``done`` | ``failed`` |
  ``cancelled``.
* :class:`JobQueue` — bounded worker pool, registry, and non-blocking queries.
* :func:`PENDING` — marker returned by :meth:`JobQueue.recoger` while a job is
  still queued or running.
* :func:`UNKNOWN_JOB` — marker returned by :meth:`JobQueue.recoger` for an
  unknown job id.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import Any


# Number of batches retained in memory.  Older batches are pruned oldest-first,
# but only when every job in the batch has reached a terminal state.
_MAX_BATCHES_RETENIDOS = 10


class JobState(StrEnum):
    """Lifecycle states of a queued job."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    """Internal per-job record."""

    job_id: str
    url: str
    batch_id: str
    state: JobState = JobState.QUEUED
    payload: dict[str, Any] | None = None
    error: str | None = None
    # Synchronization events exposed for deterministic tests.
    running: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)


def PENDING(job_id: str) -> dict[str, Any]:
    """Marker returned by ``recoger`` while a job is queued or running."""
    return {
        "ok": False,
        "error_code": "pending",
        "error": "job is still queued or running",
        "text": None,
        "job_id": job_id,
    }


def UNKNOWN_JOB(job_id: str) -> dict[str, Any]:
    """Marker returned by ``recoger`` for an unknown job id."""
    return {
        "ok": False,
        "error_code": "unknown_job",
        "error": "no job with this id",
        "text": None,
        "job_id": job_id,
    }


def CANCELLED(job_id: str) -> dict[str, Any]:
    """Payload returned by ``recoger`` for a job cancelled before it ran."""
    return {
        "ok": False,
        "error_code": "cancelled",
        "error": "job was cancelled before it ran",
        "text": None,
        "job_id": job_id,
    }


def _failure_payload(error: Exception) -> dict[str, Any]:
    """Return a minimal failure payload for a runner exception.

    The queue owns only this generic shape.  Converting it to the caller's
    server-tool payload (for example adding ``pdf_path`` / ``pdf_save_reason``
    keys) is the caller's job.
    """
    return {
        "ok": False,
        "error_code": "runner_failure",
        "error": f"{type(error).__name__}: {error}",
        "text": None,
    }


class JobQueue:
    """Run a blocking runner on a bounded pool of daemon workers.

    Args:
        runner: blocking callable ``runner(url) -> dict``.
        max_concurrentes: number of worker threads; defaults to 1.  Must be >= 1.
    """

    def __init__(self, runner: Callable[[str], dict], *, max_concurrentes: int = 1) -> None:
        if max_concurrentes < 1:
            raise ValueError("max_concurrentes must be >= 1")

        self._runner = runner
        self._max_concurrentes = max_concurrentes
        self._jobs: list[Job] = []
        self._jobs_by_id: dict[str, Job] = {}
        self._batches: dict[str, list[Job]] = {}
        self._batch_order: list[str] = []
        self._last_batch_id: str | None = None
        self._work: Queue[str | None] = Queue()
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._stop = threading.Event()
        self._stop_lock = threading.Lock()

    @property
    def max_concurrentes(self) -> int:
        """Configured worker count (read-only)."""
        return self._max_concurrentes

    def _ensure_workers(self) -> None:
        """Start exactly ``max_concurrentes`` daemon workers, lazily."""
        with self._lock:
            if self._started:
                return
            self._started = True
            for _ in range(self._max_concurrentes):
                worker = threading.Thread(target=self._worker_loop, daemon=True)
                worker.start()
                self._workers.append(worker)

    def _run_job(self, job_id: str) -> None:
        """Execute a single job and update its state under the lock."""
        job = self._jobs_by_id.get(job_id)
        if job is None:
            self._work.task_done()
            return

        with self._lock:
            if job.state == JobState.CANCELLED:
                # Cancelled while the job was still in the work queue: do not
                # run it and do not touch the payload/finished event that
                # cancel_batch already set.  task_done() is still required.
                self._work.task_done()
                return
            job.state = JobState.RUNNING
        job.running.set()
        try:
            payload = self._runner(job.url)
        except Exception as exc:
            # We catch only Exception: BaseException subclasses such as
            # KeyboardInterrupt and SystemExit signal process-level events
            # and must not be swallowed as a job-level failure.  If a runner
            # raises one, it propagates out of this worker loop, terminates
            # the daemon worker thread, and is reported by the default
            # threading exception hook.  That is acceptable because the
            # process is already exiting or the queue is being torn down;
            # the alternative is to hide such events silently.
            with self._lock:
                job.payload = _failure_payload(exc)
                job.error = str(exc)
                job.state = JobState.FAILED
        else:
            with self._lock:
                job.payload = payload
                job.state = JobState.DONE
        finally:
            job.finished.set()
            self._work.task_done()

    def _worker_loop(self) -> None:
        """Consume jobs until a sentinel value is received."""
        while True:
            # Once stop() has been called and the queue is empty we can exit.
            # We still process any real jobs that remain, so queued jobs are
            # not abandoned.
            if self._stop.is_set() and self._work.empty():
                return

            try:
                job_id = self._work.get()
            except queue.Empty:
                continue

            if job_id is None:
                # Sentinel: drain any real jobs still in the queue and exit,
                # so jobs that were queued when stop() was called are not
                # abandoned.
                self._work.task_done()
                while True:
                    try:
                        extra = self._work.get_nowait()
                    except queue.Empty:
                        break
                    if extra is None:
                        self._work.task_done()
                        continue
                    self._run_job(extra)
                return

            self._run_job(job_id)

    def _prune_batches(self) -> None:
        """Drop oldest finished batches while we exceed the retention limit.

        A batch with any queued or running job is never pruned, and pruning
        stops at the oldest busy batch so younger batches are retained while
        an older one is still active.
        """
        while len(self._batch_order) > _MAX_BATCHES_RETENIDOS:
            candidate_id = self._batch_order[0]
            batch = self._batches.get(candidate_id, [])
            if any(job.state in (JobState.QUEUED, JobState.RUNNING) for job in batch):
                # Oldest batch is still active; do not prune it or anything
                # younger while it is alive.
                break
            self._batch_order.pop(0)
            pruned = self._batches.pop(candidate_id, [])
            for job in pruned:
                self._jobs_by_id.pop(job.job_id, None)
                try:
                    self._jobs.remove(job)
                except ValueError:
                    pass

    def _submit_batch(self, urls: list[str]) -> dict[str, Any]:
        """Create one batch for ``urls`` and enqueue its jobs.

        Returns:
            A dict ``{"batch_id": str, "job_ids": list[str]}``.
        """
        self._ensure_workers()
        batch_id = uuid.uuid4().hex
        jobs: list[Job] = []
        with self._lock:
            for url in urls:
                job_id = uuid.uuid4().hex
                job = Job(job_id=job_id, url=url, batch_id=batch_id)
                self._jobs.append(job)
                self._jobs_by_id[job_id] = job
                jobs.append(job)
            self._batches[batch_id] = jobs
            self._batch_order.append(batch_id)
            self._last_batch_id = batch_id
            self._prune_batches()
        for job in jobs:
            self._work.put(job.job_id)
        return {"batch_id": batch_id, "job_ids": [job.job_id for job in jobs]}

    def submit(self, url: str) -> str:
        """Enqueue one job and return a unique, stable job id.

        The job is placed in its own single-job batch so ``submit`` remains
        compatible with callers that do not need batch scoping.
        """
        return self._submit_batch([url])["job_ids"][0]

    def submit_many(self, urls: list[str]) -> dict[str, Any]:
        """Enqueue many jobs preserving order and return their batch identity.

        Returns:
            A dict with ``batch_id`` (the id shared by every job in this
            submission) and ``job_ids`` (the per-URL identifiers in submission
            order).
        """
        return self._submit_batch(urls)

    def estado(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        """Return metadata for jobs in a single batch.

        Args:
            batch_id: id of the batch to inspect.  When ``None`` the most
                recently submitted batch is returned.

        Returns:
            Metadata records in submission order for the selected batch.
            An unknown ``batch_id`` returns an empty list rather than raising.
            The result never contains the payload text; it only exposes the
            small set of fields callers poll cheaply for a batch.
        """
        with self._lock:
            if batch_id is None:
                batch_id = self._last_batch_id
            if batch_id is None or batch_id not in self._batches:
                return []
            return [
                {
                    "job_id": job.job_id,
                    "url": job.url,
                    "state": job.state,
                    "attempts": (job.payload or {}).get("attempts"),
                    "pdf_path": (job.payload or {}).get("pdf_path"),
                    "error_code": (job.payload or {}).get("error_code"),
                }
                for job in self._batches[batch_id]
            ]

    def recoger(self, job_id: str) -> dict[str, Any]:
        """Return the raw payload for a finished job.

        * If the job is queued or running, returns :func:`PENDING` without
          blocking.
        * If the id is unknown (including pruned batches), returns
          :func:`UNKNOWN_JOB`.
        * If the runner raised, returns a failure payload with
          ``error_code == "runner_failure"``.
        """
        with self._lock:
            job = self._jobs_by_id.get(job_id)
            if job is None:
                return UNKNOWN_JOB(job_id)
            state = job.state
            payload = job.payload
        if state in (JobState.QUEUED, JobState.RUNNING):
            return PENDING(job_id)
        if state == JobState.FAILED:
            return payload or _failure_payload(RuntimeError("unknown runner error"))
        return payload or {}

    def start(self) -> None:
        """Start the worker threads; called automatically by ``submit``."""
        self._ensure_workers()

    def cancel_batch(self, batch_id: str) -> dict[str, Any]:
        """Cancel every queued job in ``batch_id``.

        Queued jobs are marked ``CANCELLED`` with a cancellation payload and
        their ``finished`` event is set so nothing blocks on them. Jobs that
        are already ``DONE``/``FAILED``/``CANCELLED`` are left untouched, and
        any ``RUNNING`` job is allowed to finish normally. Calling this method
        more than once for the same batch is safe and reports the current
        counts without error.

        Args:
            batch_id: the batch identifier returned by ``submit_many``.

        Returns:
            A dict with ``ok``, ``batch_id``, ``cancelled``,
            ``already_finished`` and ``left_running``. An unknown or pruned
            ``batch_id`` returns ``ok: false`` with ``error_code:
            "unknown_batch"``.
        """
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return {
                    "ok": False,
                    "error_code": "unknown_batch",
                    "error": "no batch with this id",
                    "batch_id": batch_id,
                }

            cancelled = 0
            already_finished = 0
            left_running = 0
            for job in batch:
                if job.state == JobState.QUEUED:
                    job.state = JobState.CANCELLED
                    job.payload = CANCELLED(job.job_id)
                    job.finished.set()
                    cancelled += 1
                elif job.state == JobState.RUNNING:
                    left_running += 1
                else:
                    # DONE, FAILED or CANCELLED.
                    already_finished += 1

            return {
                "ok": True,
                "batch_id": batch_id,
                "cancelled": cancelled,
                "already_finished": already_finished,
                "left_running": left_running,
            }

    def stop(self) -> None:
        """Stop the queue idempotently with a bounded timeout.

        Running jobs are allowed to finish, and queued jobs are drained before
        workers exit.  Because the join timeout is bounded, ``stop()`` may
        return while a long-running job is still active; the daemon worker
        continues draining and exits once the queue is empty.
        """
        with self._stop_lock:
            if self._stop.is_set():
                return
            # Wake each worker with a sentinel so idle workers drain and exit.
            for _ in range(self._max_concurrentes):
                try:
                    self._work.put_nowait(None)
                except Exception:
                    pass
            self._stop.set()

        with self._lock:
            workers = list(self._workers)

        for worker in workers:
            try:
                worker.join(timeout=2.0)
            except Exception:
                pass
