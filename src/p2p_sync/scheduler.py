"""Minimal cron scheduler for running the sync flows on a timetable.

    scheduler = Scheduler(tz=ZoneInfo("Asia/Manila"))

    @scheduler.cron("*/15 * * * *")
    def sync_invoices() -> None: ...

    scheduler.run_forever()

Five-field expressions are standard cron. Six fields means the first one is seconds,
same as Azure Functions timer triggers, so schedules can be copied across as-is.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import Any

from croniter import croniter

from p2p_sync.core.logging import log_context

logger = logging.getLogger(__name__)

JobFunc = Callable[[], Any]


@dataclass
class Job:
    name: str
    expression: str
    func: JobFunc
    next_run: datetime
    last_run: datetime | None = None
    last_error: Exception | None = None
    runs: int = 0
    failures: int = 0


def _has_seconds(expression: str) -> bool:
    return len(expression.split()) == 6


def next_fire_time(expression: str, after: datetime) -> datetime:
    itr = croniter(expression, after, second_at_beginning=_has_seconds(expression))
    result: datetime = itr.get_next(datetime)
    return result


class Scheduler:
    def __init__(self, tz: tzinfo = UTC, clock: Callable[[], datetime] | None = None) -> None:
        self.tz = tz
        self._clock = clock or (lambda: datetime.now(tz))
        self._jobs: dict[str, Job] = {}

    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def now(self) -> datetime:
        return self._clock().astimezone(self.tz)

    def add_job(
        self,
        func: JobFunc,
        expression: str,
        *,
        name: str | None = None,
        run_on_start: bool = False,
    ) -> Job:
        job_name: str = name or getattr(func, "__name__", None) or repr(func)
        if job_name in self._jobs:
            raise ValueError(f"a job called {job_name!r} is already scheduled")
        if not croniter.is_valid(expression, second_at_beginning=_has_seconds(expression)):
            raise ValueError(f"invalid cron expression for {job_name!r}: {expression!r}")

        now = self.now()
        first_run = now if run_on_start else next_fire_time(expression, now)
        job = Job(job_name, expression, func, next_run=first_run)
        self._jobs[job_name] = job
        logger.info("scheduled %s (%s), first run %s", job_name, expression, first_run.isoformat())
        return job

    def cron(
        self, expression: str, *, name: str | None = None, run_on_start: bool = False
    ) -> Callable[[JobFunc], JobFunc]:
        def register(func: JobFunc) -> JobFunc:
            self.add_job(func, expression, name=name, run_on_start=run_on_start)
            return func

        return register

    def run_pending(self) -> list[Job]:
        """Run every job that is due and return the ones that ran."""
        due = [job for job in self._jobs.values() if job.next_run <= self.now()]
        for job in due:
            self._run(job)
        return due

    def run_forever(self, stop: threading.Event | None = None, max_sleep: float = 60.0) -> None:
        stop = stop or threading.Event()
        logger.info("scheduler started with %d job(s)", len(self._jobs))
        while not stop.is_set():
            self.run_pending()
            stop.wait(self._seconds_until_next_run(max_sleep))
        logger.info("scheduler stopped")

    def _seconds_until_next_run(self, cap: float) -> float:
        if not self._jobs:
            return cap
        soonest = min(job.next_run for job in self._jobs.values())
        return max(0.0, min(cap, (soonest - self.now()).total_seconds()))

    def _run(self, job: Job) -> None:
        started = self.now()
        t0 = time.perf_counter()
        with log_context(job=job.name):
            try:
                job.func()
            except Exception as exc:
                job.failures += 1
                job.last_error = exc
                logger.exception("job %s failed", job.name)
            else:
                job.last_error = None
                logger.info("job %s finished in %.1fs", job.name, time.perf_counter() - t0)
        job.runs += 1
        job.last_run = started
        # Schedule from *now*, not from the slot we were due at. If a run overruns a few
        # slots we want one catch-up run, not a burst of them.
        job.next_run = next_fire_time(job.expression, self.now())
