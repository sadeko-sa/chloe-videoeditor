"""
jobs.py - The job queue: jobs wait here and background workers render them one after another
(or several at once). No Tk in here; listeners are called from worker threads.
"""

from __future__ import annotations

import enum
import itertools
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .ffmpeg import Export

WORKERS_RANGE = (1, 8)
"""Allowed number of exports rendered at the same time."""

THREADS_RANGE = (0, 64)
"""Allowed thread limit per export; 0 = FFmpeg decides."""


class QueueError(Exception):
    """A job cannot be queued (same output file already queued, queue closed)."""


class JobState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def active(self) -> bool:
        """@return True while the job still waits, runs or is paused"""
        return self in (JobState.QUEUED, JobState.RUNNING, JobState.PAUSED)

    @property
    def resettable(self) -> bool:
        """@return True once the job has ended (done, failed or stopped); only then it can be reset"""
        return not self.active


@dataclass
class ExportJob:
    """
    One entry of the queue.

    @param id        number, unique within the queue
    @param export    what to run
    @param state     where the job is
    @param error     message of a failed job, else ""
    @param progress  0..1 while running
    @param worker    number of the worker that runs (or ran) it, 0 = none yet
    @param started   time.monotonic() when it started running, 0 = not yet
    @param finished  time.monotonic() when it ended, 0 = not yet
    @param paused_at     time.monotonic() when the current pause began, 0 = not paused
    @param paused_total  seconds spent paused before the current pause
    """
    id: int
    export: Export
    state: JobState = JobState.QUEUED
    error: str = ""
    progress: float = 0.0
    worker: int = 0
    started: float = 0.0
    finished: float = 0.0
    paused_at: float = 0.0
    paused_total: float = 0.0

    @property
    def elapsed(self) -> float:
        """@return seconds spent rendering so far (time paused not counted), 0 if it has not started"""
        if not self.started:
            return 0.0
        now = time.monotonic()
        idle = self.paused_total + (now - self.paused_at if self.paused_at else 0.0)
        return max((self.finished or now) - self.started - idle, 0.0)

    def end_pause(self, now: float) -> None:
        """Count a pause that is going on as over at `now`."""
        if self.paused_at:
            self.paused_total += now - self.paused_at
            self.paused_at = 0.0

    @property
    def out_path(self) -> str:
        return self.export.out_path

    @property
    def name(self) -> str:
        """@return file name of the output"""
        return os.path.basename(self.export.out_path)

    def set_progress(self, fraction: float) -> None:
        self.progress = fraction


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class JobQueue:
    """
    Jobs in, background workers out. Workers are numbered from 1; they can be added or retired while running.

        queue = JobQueue(workers=1, threads=0, on_change=lambda job: ...)
        job = queue.add(export)             # waits: a new queue is not started
        queue.start_all()                   # workers take the waiting jobs, also those added later
        queue.pause_all()                   # freeze what renders, take nothing new
        queue.resume_all()                  # go on
        queue.stop_all()                    # stop what renders, take nothing new
        queue.cancel(job.id)                # one job only
        queue.shutdown()

    The queue begins not started: jobs wait until start_all(). start_all/stop_all/pause_all/resume_all act on
    the whole queue; pause/resume/cancel act on one job.

    on_change(job) is called after a job was added or changed state, from any thread and never with the
    queue lock held. Progress is not reported that way; read ExportJob.progress.
    """

    def __init__(self, workers: int = 1, threads: int = 0,
                 on_change: Optional[Callable[[ExportJob], None]] = None):
        """
        @param workers    exports rendered at the same time (WORKERS_RANGE)
        @param threads    thread limit of each export, 0 = FFmpeg decides (THREADS_RANGE)
        @param on_change  listener, see class text
        """
        self._cond = threading.Condition()
        self._jobs: List[ExportJob] = []
        self._pool: List[threading.Thread] = []
        self._slots: Dict[int, Optional[ExportJob]] = {}     # worker number -> the job it runs now
        self._ids = itertools.count(1)
        self._target = self._clamp(workers, WORKERS_RANGE)
        self._threads = self._clamp(threads, THREADS_RANGE)
        self._closed = False
        self._started = False
        self._paused = False
        self._on_change = on_change

    @staticmethod
    def _clamp(value: int, bounds: tuple) -> int:
        return max(bounds[0], min(bounds[1], int(value)))

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    @property
    def threads(self) -> int:
        """@return the thread limit of each export (0 = FFmpeg decides)"""
        return self._threads

    @property
    def started(self) -> bool:
        """@return True from start_all() until stop_all(); the workers take waiting jobs (unless paused)"""
        return self._started

    @property
    def paused(self) -> bool:
        """@return True from pause_all() until resume_all() or start_all()"""
        return self._paused

    def start_all(self) -> None:
        """Start the queue: waiting jobs are taken one after the other (also jobs added later); paused jobs go on."""
        with self._cond:
            self._started = True
            resumed = self._resume_locked()
            self._cond.notify_all()
        for job in resumed:
            self._changed(job)

    def stop_all(self) -> None:
        """Stop the queue: every job that renders or is paused is stopped (Reset can queue it again) and no waiting
        job is taken. The waiting jobs stay in the list."""
        with self._cond:
            self._started = self._paused = False
            stopped = [j for j in self._jobs if j.state in (JobState.RUNNING, JobState.PAUSED)]
            for job in stopped:
                job.export.cancel()                 # the job turns CANCELLED when its worker sees the process end
        for job in stopped:
            self._changed(job)

    def pause_all(self) -> None:
        """Pause the queue: every job that renders is frozen and no waiting job is taken. resume_all() goes on."""
        with self._cond:
            self._paused = True
            now = time.monotonic()
            frozen = [j for j in self._jobs if j.state is JobState.RUNNING]
            for job in frozen:
                job.export.pause()
                job.state, job.paused_at = JobState.PAUSED, now
        for job in frozen:
            self._changed(job)

    def resume_all(self) -> None:
        """Let the paused queue go on: paused jobs continue and waiting jobs are taken again."""
        with self._cond:
            resumed = self._resume_locked()
            self._cond.notify_all()
        for job in resumed:
            self._changed(job)

    def _resume_locked(self) -> List[ExportJob]:
        """End the queue pause and unfreeze the paused jobs (lock held). @return the jobs that go on"""
        self._paused = False
        now = time.monotonic()
        resumed = [j for j in self._jobs if j.state is JobState.PAUSED]
        for job in resumed:
            job.export.resume()
            job.end_pause(now)
            job.state = JobState.RUNNING
        return resumed

    def set_workers(self, count: int) -> None:
        """Change the number of workers; a worker that is busy finishes its job before it retires."""
        with self._cond:
            self._target = self._clamp(count, WORKERS_RANGE)
            self._spawn_locked()
            self._cond.notify_all()                 # idle surplus workers retire

    def set_threads(self, count: int) -> None:
        """Thread limit for the exports that start from now on (0 = FFmpeg decides)."""
        self._threads = self._clamp(count, THREADS_RANGE)

    # ------------------------------------------------------------------ #
    # Jobs
    # ------------------------------------------------------------------ #

    def add(self, export: Export) -> ExportJob:
        """
        Queue an export.

        @raise QueueError if an active job writes the same file, or the queue was shut down
        """
        with self._cond:
            if self._closed:
                raise QueueError("the queue is closed")
            if any(j.state.active and _same_file(j.out_path, export.out_path) for j in self._jobs):
                raise QueueError(export.out_path)
            job = ExportJob(next(self._ids), export)
            export.on_progress = job.set_progress
            self._jobs.append(job)
            self._spawn_locked()
            self._cond.notify()
        self._changed(job)
        return job

    def jobs(self) -> List[ExportJob]:
        """@return the jobs in the order they were added (a copy)"""
        with self._cond:
            return list(self._jobs)

    def workers(self) -> List[Tuple[int, Optional[ExportJob]]]:
        """@return (worker number, the job it runs or None if idle), by number"""
        with self._cond:
            return sorted(self._slots.items())

    def active_count(self) -> int:
        """@return number of jobs that wait or run"""
        with self._cond:
            return sum(1 for j in self._jobs if j.state.active)

    def _find_locked(self, job_id: int) -> Optional[ExportJob]:
        return next((j for j in self._jobs if j.id == job_id), None)

    def pause(self, job_id: int) -> None:
        """Freeze a running job (its process is suspended, nothing is lost). Others: no effect."""
        with self._cond:
            job = self._find_locked(job_id)
            if job is None or job.state is not JobState.RUNNING:
                return
            job.export.pause()
            job.state, job.paused_at = JobState.PAUSED, time.monotonic()
        self._changed(job)

    def resume(self, job_id: int) -> None:
        """Let a paused job go on. Others: no effect."""
        with self._cond:
            job = self._find_locked(job_id)
            if job is None or job.state is not JobState.PAUSED:
                return
            job.export.resume()
            job.end_pause(time.monotonic())
            job.state = JobState.RUNNING
        self._changed(job)

    def cancel(self, job_id: int) -> None:
        """Stop a waiting job (it never starts), a running or a paused one (its process is killed). Others: no effect."""
        with self._cond:
            job = self._find_locked(job_id)
            if job is None or not job.state.active:
                return
            job.export.cancel()
            if job.state is JobState.QUEUED:
                job.state = JobState.CANCELLED
            # a running job turns CANCELLED when its worker sees the process end
        self._changed(job)

    def reset(self, job_id: int) -> None:
        """
        Put a job that is done, failed or stopped back to the end state "waiting" so it renders again.
        A job that waits, runs or is paused cannot be reset (stop it first). Others: no effect.

        @raise QueueError if an active job writes the same file, or the queue was shut down
        """
        with self._cond:
            job = self._find_locked(job_id)
            if job is None or not job.state.resettable:
                return
            if self._closed:
                raise QueueError("the queue is closed")
            if any(j is not job and j.state.active and _same_file(j.out_path, job.out_path) for j in self._jobs):
                raise QueueError(job.out_path)
            job.export.reset()
            job.state, job.error, job.progress, job.worker = JobState.QUEUED, "", 0.0, 0
            job.started = job.finished = job.paused_at = job.paused_total = 0.0
            self._spawn_locked()
            self._cond.notify()
        self._changed(job)

    def remove(self, job_id: int) -> None:
        """Take a job out of the list; a job that waits, runs or is paused is stopped first."""
        with self._cond:
            job = self._find_locked(job_id)
            if job is None:
                return
            if job.state.active:
                job.export.cancel()
            self._jobs.remove(job)
        self._changed(job)

    def clear_finished(self) -> None:
        """Drop done, failed and cancelled jobs from the list."""
        with self._cond:
            self._jobs = [j for j in self._jobs if j.state.active]

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel everything, refuse new jobs and wait (at most `timeout` seconds) for the workers to clean up."""
        with self._cond:
            self._closed = True
            for job in self._jobs:
                if job.state.active:
                    job.export.cancel()
                    if job.state is JobState.QUEUED:
                        job.state = JobState.CANCELLED
            pool = list(self._pool)
            self._cond.notify_all()
        for worker in pool:
            worker.join(timeout)

    # ------------------------------------------------------------------ #
    # Workers
    # ------------------------------------------------------------------ #

    def _changed(self, job: ExportJob) -> None:
        if self._on_change:
            self._on_change(job)

    def _spawn_locked(self) -> None:
        """Start the missing workers 1..target (call with the lock held)."""
        for number in range(1, self._target + 1):
            if number in self._slots or self._closed:
                continue
            worker = threading.Thread(target=self._work, args=(number,), name="worker-%d" % number, daemon=True)
            self._slots[number] = None
            self._pool.append(worker)
            worker.start()

    def _take_locked(self, number: int) -> Optional[ExportJob]:
        """
        Wait for the next waiting job (lock held).

        @return the job, marked running; None if worker `number` has to end (queue closed or worker not wanted)
        """
        while True:
            if self._closed or number > self._target:
                del self._slots[number]
                self._pool.remove(threading.current_thread())
                return None
            job = next((j for j in self._jobs if j.state is JobState.QUEUED), None) if self._started and not self._paused else None
            if job:
                job.state, job.worker, job.started = JobState.RUNNING, number, time.monotonic()
                self._slots[number] = job
                return job
            self._cond.wait()

    def _work(self, number: int) -> None:
        while True:
            with self._cond:
                job = self._take_locked(number)
            if job is None:
                return
            self._changed(job)
            error = ""
            try:
                error = job.export.run(self._threads) or ""
            except Exception as exc:                # a worker must survive whatever one job does
                error = "%s: %s" % (type(exc).__name__, exc)
            with self._cond:
                if job.export.cancelled:
                    job.state = JobState.CANCELLED
                elif error:
                    job.state, job.error = JobState.FAILED, error
                else:
                    job.state, job.progress = JobState.DONE, 1.0
                job.finished = time.monotonic()
                job.end_pause(job.finished)
                self._slots[number] = None
            self._changed(job)
