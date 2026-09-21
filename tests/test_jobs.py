"""Tests for the bounded job queue module.

The queue is deliberately ignorant of CENDOJ and HTTP: every behaviour is
driven by a caller-supplied blocking runner so the tests can run offline.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest

from navaja.jobs import JobQueue, JobState, PENDING, UNKNOWN_JOB, _failure_payload


def make_runner() -> tuple[Callable[[str], dict], Callable[[], None], Callable[[], None]]:
    """Return a controllable runner plus resume/halt controls.

    The returned runner blocks until ``resume`` is called.  The optional
    ``halt`` flag makes every call raise an exception.  ``busy`` is set while
    the runner is executing and cleared when it returns.
    """
    gate = threading.Event()
    fail = threading.Event()
    busy = threading.Event()
    lock = threading.Lock()

    def runner(url: str) -> dict:
        with lock:
            if fail.is_set():
                raise RuntimeError(f"boom {url}")
        busy.set()
        try:
            gate.wait(timeout=5)
        finally:
            busy.clear()
        return {"url": url, "payload": f"ok {url}"}

    def resume() -> None:
        gate.set()

    def halt() -> None:
        fail.set()
        resume()

    return runner, resume, halt


def job_ids_are_unique(ids: list[str]) -> bool:
    return len(set(ids)) == len(ids)


@pytest.fixture
def queue() -> JobQueue:
    q = JobQueue(runner=lambda url: {"ok": True})
    yield q
    q.stop()


def test_invalid_concurrency() -> None:
    with pytest.raises(ValueError):
        JobQueue(runner=lambda url: {}, max_concurrentes=0)
    with pytest.raises(ValueError):
        JobQueue(runner=lambda url: {}, max_concurrentes=-1)


def test_max_concurrentes_public_accessor() -> None:
    """JobQueue exposes its configured worker count as a public read-only value."""
    q1 = JobQueue(runner=lambda url: {})
    try:
        assert q1.max_concurrentes == 1
    finally:
        q1.stop()

    q3 = JobQueue(runner=lambda url: {}, max_concurrentes=3)
    try:
        assert q3.max_concurrentes == 3
    finally:
        q3.stop()


def test_job_state_values_and_casing() -> None:
    """JobState matches the repository StrEnum convention: uppercase members,
    lowercase string values."""
    assert JobState.QUEUED == "queued"
    assert JobState.RUNNING == "running"
    assert JobState.DONE == "done"
    assert JobState.FAILED == "failed"
    assert JobState.QUEUED.value == "queued"
    assert JobState.RUNNING.value == "running"
    assert JobState.DONE.value == "done"
    assert JobState.FAILED.value == "failed"


def test_failure_payload_is_generic_not_tool_shaped() -> None:
    """The queue owns a minimal failure shape; PDF keys belong to the caller."""
    payload = _failure_payload(RuntimeError("something went wrong"))
    assert set(payload.keys()) == {"ok", "error_code", "error", "text"}
    assert payload["ok"] is False
    assert payload["error_code"] == "runner_failure"
    assert "something went wrong" in payload["error"]
    assert payload["text"] is None
    assert "pdf_path" not in payload
    assert "pdf_save_reason" not in payload


def test_submit_returns_unique_stable_id(queue: JobQueue) -> None:
    id1 = queue.submit("http://a")
    id2 = queue.submit("http://a")
    id3 = queue.submit("http://b")

    assert id1 and isinstance(id1, str)
    assert id1 != id2
    assert id1 != id3


def test_submit_many_returns_unique_ids(queue: JobQueue) -> None:
    ids = queue.submit_many(["u1", "u2", "u3"])
    assert len(ids) == 3
    assert job_ids_are_unique(ids)


def test_jobs_run_fifio(queue: JobQueue) -> None:
    order: list[str] = []
    started = threading.Event()

    def runner(url: str) -> dict:
        order.append(url)
        if url == "last":
            started.set()
        return {"url": url}

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("first")
        q.submit("second")
        q.submit("last")
        started.wait(timeout=5)
        q.stop()
        assert order == ["first", "second", "last"]
    finally:
        q.stop()


def test_concurrency_bound_is_real() -> None:
    """With max_concurrentes=1 two jobs must never overlap in the runner."""
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("a")
        q.submit("b")
        # Wait until the first job is inside the runner.
        assert q._jobs  # sanity: jobs exist
        q._jobs[0].running.wait(timeout=5)
        # While the first is running the second must still be queued.
        state = q.estado()
        assert len(state) == 2
        assert state[0]["state"] == JobState.RUNNING
        assert state[1]["state"] == JobState.QUEUED
        resume()
        q.stop()
    finally:
        q.stop()


def test_worker_pool_does_not_grow_per_job() -> None:
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit_many(["a", "b", "c"])
        # Wait until at least one job has entered the runner.
        assert q._jobs[0].running.wait(timeout=5)
        # There can only ever be max_concurrentes workers.
        alive = sum(1 for t in q._workers if t.is_alive())
        assert alive == 1
        resume()
        q.stop()
        # Same worker count afterwards; none were spawned per job.
        assert len(q._workers) == 1
    finally:
        q.stop()


def test_estado_leaks_no_payload_text(queue: JobQueue) -> None:
    done = threading.Event()

    def runner(url: str) -> dict:
        result = {"text": "SECRET TEXT", "attempts": 2, "pdf_path": "/tmp/x.pdf", "error_code": "E1"}
        if url == "finish":
            done.set()
        return result

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("first")
        q.submit("finish")
        done.wait(timeout=5)
        q.stop()
        for entry in q.estado():
            assert set(entry.keys()) == {"job_id", "url", "state", "attempts", "pdf_path", "error_code"}
            for value in entry.values():
                assert "SECRET TEXT" not in str(value)
            assert entry["attempts"] == 2
            assert entry["pdf_path"] == "/tmp/x.pdf"
            assert entry["error_code"] == "E1"
    finally:
        q.stop()


def test_estado_defaults_missing_payload_fields_to_none(queue: JobQueue) -> None:
    done = threading.Event()

    def runner(url: str) -> dict:
        done.set()
        return {"ok": True}

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("x")
        done.wait(timeout=5)
        q.stop()
        entry = q.estado()[0]
        assert entry["attempts"] is None
        assert entry["pdf_path"] is None
        assert entry["error_code"] is None
    finally:
        q.stop()


def test_recoger_pending_while_running() -> None:
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        job_id = q.submit("a")
        # Wait until the runner is executing this job.
        q._jobs[0].running.wait(timeout=5)
        assert q.recoger(job_id) == PENDING(job_id)
        resume()
        q.stop()
    finally:
        q.stop()


def test_recoger_returns_payload_when_done() -> None:
    done = threading.Event()

    def runner(url: str) -> dict:
        result = {"ok": True, "text": "hello"}
        done.set()
        return result

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        job_id = q.submit("x")
        done.wait(timeout=5)
        q.stop()
        assert q.recoger(job_id) == {"ok": True, "text": "hello"}
    finally:
        q.stop()


def test_recoger_unknown_job() -> None:
    q = JobQueue(runner=lambda url: {"ok": True})
    try:
        assert q.recoger("does-not-exist") == UNKNOWN_JOB("does-not-exist")
    finally:
        q.stop()


def test_recoger_failure_shape() -> None:
    error_message = "something went wrong"

    def runner(url: str) -> dict:
        raise RuntimeError(error_message)

    done = threading.Event()

    def wrapped(url: str) -> dict:
        try:
            return runner(url)
        finally:
            done.set()

    q = JobQueue(runner=wrapped, max_concurrentes=1)
    try:
        job_id = q.submit("x")
        done.wait(timeout=5)
        q.stop()
        result = q.recoger(job_id)
        assert result["ok"] is False
        assert result["error_code"] == "runner_failure"
        assert error_message in result["error"]
        assert result["text"] is None
    finally:
        q.stop()


def test_runner_exception_does_not_kill_worker_and_later_jobs_complete() -> None:
    first = threading.Event()
    second_done = threading.Event()

    def runner(url: str) -> dict:
        if url == "bad":
            first.set()
            raise RuntimeError("bad job")
        result = {"ok": True, "url": url}
        if url == "good":
            second_done.set()
        return result

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        bad_id = q.submit("bad")
        good_id = q.submit("good")
        first.wait(timeout=5)
        second_done.wait(timeout=5)
        q.stop()
        assert q.recoger(good_id) == {"ok": True, "url": "good"}
        failure = q.recoger(bad_id)
        assert failure["ok"] is False
        assert failure["error_code"] == "runner_failure"
    finally:
        q.stop()


def test_concurrent_submit_runs_every_job() -> None:
    lock = threading.Lock()
    seen: set[str] = set()
    barrier = threading.Barrier(10)

    def runner(url: str) -> dict:
        with lock:
            seen.add(url)
        return {"url": url}

    q = JobQueue(runner=runner, max_concurrentes=2)
    try:
        threads: list[threading.Thread] = []
        urls = [f"url-{i}" for i in range(10)]
        collected: list[str | None] = [None] * 10

        def worker(i: int, url: str) -> None:
            barrier.wait(timeout=5)
            collected[i] = q.submit(url)

        for i, url in enumerate(urls):
            t = threading.Thread(target=worker, args=(i, url), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=5)

        q.stop()
        assert all(jid is not None for jid in collected)
        assert job_ids_are_unique([jid for jid in collected if jid is not None])
        assert seen == set(urls)
    finally:
        q.stop()


def test_stop_is_idempotent_and_safe_from_worker_thread() -> None:
    calls: list[bool] = []
    stopped = threading.Event()

    def runner(url: str) -> dict:
        # Calling stop from inside the runner must not deadlock.
        calls.append(True)
        q.stop()
        stopped.set()
        return {"ok": True}

    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("x")
        stopped.wait(timeout=5)
        # Idempotent: second stop must not raise.
        q.stop()
        q.stop()
    finally:
        q.stop()


def test_stop_drains_jobs_still_queued() -> None:
    """stop() must not leave unstarted jobs in ``queued`` forever."""
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        first_id = q.submit("a")
        second_id = q.submit("b")
        q._jobs[0].running.wait(timeout=5)

        q.stop()

        # The first job was already running; the second is still queued but
        # has not been abandoned.
        assert q.recoger(second_id) == PENDING(second_id)
        assert q.estado()[1]["state"] == JobState.QUEUED

        # Release the first job so the daemon worker drains the queued one.
        resume()
        q._jobs[1].finished.wait(timeout=5)
        assert q.recoger(first_id) == {"url": "a", "payload": "ok a"}
        assert q.recoger(second_id) == {"url": "b", "payload": "ok b"}
    finally:
        q.stop()


def test_marker_returned_is_independent_of_future_calls() -> None:
    """Mutating a marker returned by recoger must not affect later calls."""
    q = JobQueue(runner=lambda url: {"ok": True})
    try:
        unknown = q.recoger("nope")
        unknown["extra"] = "mutated"
        assert q.recoger("nope") == UNKNOWN_JOB("nope")

        runner, resume, _ = make_runner()
        q2 = JobQueue(runner=runner, max_concurrentes=1)
        try:
            job_id = q2.submit("x")
            q2._jobs[0].running.wait(timeout=5)
            pending = q2.recoger(job_id)
            pending["extra"] = "mutated"
            assert q2.recoger(job_id) == PENDING(job_id)
            resume()
            q2._jobs[0].finished.wait(timeout=5)
        finally:
            q2.stop()
    finally:
        q.stop()


def test_markers_carry_their_job_id() -> None:
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        assert q.recoger("missing")["job_id"] == "missing"

        job_id = q.submit("x")
        q._jobs[0].running.wait(timeout=5)
        assert q.recoger(job_id)["job_id"] == job_id
        resume()
        q._jobs[0].finished.wait(timeout=5)
    finally:
        q.stop()


def test_state_reads_are_safe_while_workers_change_state() -> None:
    """Polling estado/recoger concurrently with running workers must not crash
    or observe impossible states, using only bounded waits."""
    runner, resume, _ = make_runner()
    urls = [f"url-{i}" for i in range(12)]
    q = JobQueue(runner=runner, max_concurrentes=4)
    errors: list[BaseException] = []

    try:
        job_ids = q.submit_many(urls)

        def reader() -> None:
            try:
                for _ in range(150):
                    for entry in q.estado():
                        state = entry["state"]
                        assert state in {
                            JobState.QUEUED,
                            JobState.RUNNING,
                            JobState.DONE,
                            JobState.FAILED,
                        }
                        # A queued/running job must not already carry a final payload.
                        if state in (JobState.QUEUED, JobState.RUNNING):
                            assert entry["error_code"] is None
                            assert entry["pdf_path"] is None
                            assert entry["attempts"] is None
                    for jid in job_ids:
                        result = q.recoger(jid)
                        assert result["job_id"] == jid
            except BaseException as exc:
                errors.append(exc)

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for t in readers:
            t.start()
        for t in readers:
            t.join(timeout=10)

        resume()
        for job in q._jobs:
            job.finished.wait(timeout=5)

        assert not errors
        assert all(job.state == JobState.DONE for job in q._jobs)
    finally:
        q.stop()


def test_threads_are_daemon() -> None:
    runner, resume, _ = make_runner()
    q = JobQueue(runner=runner, max_concurrentes=1)
    try:
        q.submit("x")
        q._jobs[0].running.wait(timeout=5)
        for worker in q._workers:
            assert worker.daemon is True
        resume()
        q.stop()
    finally:
        q.stop()


def test_job_id_not_derived_from_url() -> None:
    q = JobQueue(runner=lambda url: {"url": url})
    try:
        job_id = q.submit("http://example.com")
        assert job_id != "http://example.com"
        # URL should not be a substring of the id.
        assert "example.com" not in job_id
    finally:
        q.stop()
