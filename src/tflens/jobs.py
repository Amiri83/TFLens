"""Background Discover runs, followed by the progress screen (one run per key).

A job runs :func:`pipeline.discover` in a worker thread. Its state (current phase and
step, the phases' outcome, a log tail, the result) is polled by the progress screen;
**Cancel** sets an event that the next :meth:`JobContext.step` turns into
:class:`pipeline.Cancelled`, so the worker stops between two steps, before anything is
written (the results are written by the pipeline's last step). :meth:`JobManager.shutdown`
cancels and joins every worker, so none is left behind when the server stops.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .pipeline import PHASES, Cancelled

log = logging.getLogger(__name__)

LOG_TAIL = 400
KEEP_FINISHED = 20


class JobBusy(RuntimeError):
    """A job with the same key is still running (``job``: its status)."""

    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__("a Discover run is already in progress for this repository")
        self.job = job


class JobFailed(RuntimeError):
    """A failure whose message is safe to show (no secrets, no paths outside the repo)."""


class Job:
    def __init__(self, key: str, label: str) -> None:
        self.id = secrets.token_hex(8)
        self.key = key
        self.label = label
        self.started = time.monotonic()
        self.ended: float | None = None
        self.state = "running"  # running | done | cancelled | error
        self.phase = ""
        self.step = "starting"
        self.phases = [
            {"name": n, "label": label_, "status": "pending", "detail": ""} for n, label_ in PHASES
        ]
        self.log: deque[str] = deque(maxlen=LOG_TAIL)
        self.messages: list[dict[str, str]] = []
        self.result: dict[str, Any] = {}
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.state == "running"

    def status(self) -> dict[str, Any]:
        with self.lock:
            end = self.ended if self.ended is not None else time.monotonic()
            return {
                "id": self.id,
                "key": self.key,
                "label": self.label,
                "running": self.running,
                "state": self.state,
                "cancel_requested": self.cancel_event.is_set(),
                "phase": self.phase,
                "step": self.step,
                "phases": [dict(p) for p in self.phases],
                "elapsed": int(end - self.started),
                "log": list(self.log),
                "messages": list(self.messages),
                "result": dict(self.result),
            }


class JobContext:
    """The :class:`pipeline.Progress` of one job."""

    def __init__(self, job: Job) -> None:
        self.job = job

    def _phase(self, name: str) -> dict[str, Any] | None:
        return next((p for p in self.job.phases if p["name"] == name), None)

    def check(self) -> None:
        if self.job.cancel_event.is_set():
            raise Cancelled()

    def phase(self, name: str, label: str) -> None:
        self.check()
        with self.job.lock:
            self.job.phase = name
            p = self._phase(name)
            if p is not None:
                p["status"] = "running"
            self.job.log.append(f"== {label}")

    def phase_done(self, name: str, status: str, detail: str = "") -> None:
        with self.job.lock:
            p = self._phase(name)
            if p is not None:
                p["status"], p["detail"] = status, detail

    def step(self, text: str) -> None:
        self.check()
        with self.job.lock:
            self.job.step = text
            self.job.log.append(f"   {text}")

    def log(self, text: str) -> None:
        with self.job.lock:
            self.job.log.append(text)

    def message(self, category: str, text: str, link_url: str = "", link_text: str = "") -> None:
        with self.job.lock:
            entry = {"category": category, "text": text}
            if link_url:
                entry.update(link_url=link_url, link_text=link_text or link_url)
            self.job.messages.append(entry)


Work = Callable[[JobContext], "dict[str, Any] | None"]


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, key: str, label: str, work: Work, *, background: bool = True) -> dict[str, Any]:
        """Start ``work`` (raises :class:`JobBusy` while a job of ``key`` runs). Without
        ``background`` the job runs to the end in the caller's thread."""
        with self._lock:
            for j in self._jobs.values():
                if j.key == key and j.running:
                    raise JobBusy(j.status())
            job = Job(key, label)
            self._jobs[job.id] = job
            self._prune()
        if background:
            job.thread = threading.Thread(
                target=self._run, args=(job, work), name=f"tflens-discover-{job.id}", daemon=True
            )
            job.thread.start()
        else:
            self._run(job, work)
        return job.status()

    def _run(self, job: Job, work: Work) -> None:
        ctx = JobContext(job)
        state = "done"
        try:
            result = work(ctx) or {}
            with job.lock:
                job.result = result
        except Cancelled:
            state = "cancelled"
            ctx.message("warn", "Cancelled: the run stopped cleanly; nothing was written.")
            log.info("discover job %s (%s) cancelled", job.id, job.key)
        except JobFailed as exc:
            state = "error"
            ctx.message("error", str(exc))
            log.warning("discover job %s (%s) failed: %s", job.id, job.key, exc)
        except Exception as exc:
            state = "error"
            ctx.message("error", f"Discover failed ({type(exc).__name__}): see the log.")
            log.exception("discover job %s (%s) failed", job.id, job.key)
        with job.lock:
            for p in job.phases:
                if p["status"] == "running":  # stopped in this phase; later ones not run
                    p["status"] = "cancelled" if state == "cancelled" else "failed"
                elif p["status"] == "pending" and state != "done":
                    p["status"] = "not run"
            job.state = state
            job.step = {"done": "finished", "cancelled": "cancelled", "error": "failed"}[state]
            job.ended = time.monotonic()
            job.log.append(f"== {job.step}")

    def _prune(self) -> None:
        done = [j for j in self._jobs.values() if not j.running]
        for j in sorted(done, key=lambda j: j.started)[:-KEEP_FINISHED]:
            self._jobs.pop(j.id, None)

    def get(self, job_id: str, key: str | None = None) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or (key is not None and job.key != key):
            return None
        return job

    def status(self, job_id: str, key: str | None = None) -> dict[str, Any] | None:
        job = self.get(job_id, key)
        return job.status() if job else None

    def current(self, key: str) -> dict[str, Any] | None:
        """The running job of ``key``, else its latest one."""
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.key == key]
        if not jobs:
            return None
        running = [j for j in jobs if j.running]
        return (running or sorted(jobs, key=lambda j: j.started))[-1].status()

    def cancel(self, job_id: str, key: str | None = None) -> bool:
        job = self.get(job_id, key)
        if job is None or not job.running:
            return False
        job.cancel_event.set()
        with job.lock:
            job.log.append("== cancel requested (stops after the current step)")
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        if job.thread is not None:
            job.thread.join(timeout)
        return job.status()

    def shutdown(self, timeout: float = 10.0) -> list[str]:
        """Cancel every running job and wait for its worker (up to ``timeout`` in all)."""
        with self._lock:
            running = [j for j in self._jobs.values() if j.running]
        for j in running:
            j.cancel_event.set()
        deadline = time.monotonic() + timeout
        for j in running:
            if j.thread is not None:
                j.thread.join(max(0.0, deadline - time.monotonic()))
        return [j.id for j in running]
