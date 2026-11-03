import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from p2p_sync.core.logging import current_log_context
from p2p_sync.scheduler import Scheduler, next_fire_time

MANILA = ZoneInfo("Asia/Manila")


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 3, 8, 7, tzinfo=MANILA))


@pytest.fixture
def scheduler(clock: FakeClock) -> Scheduler:
    return Scheduler(tz=MANILA, clock=clock)


def test_next_fire_time_five_and_six_fields() -> None:
    start = datetime(2026, 11, 3, 8, 7, 12, tzinfo=MANILA)

    assert next_fire_time("*/15 * * * *", start) == datetime(2026, 11, 3, 8, 15, tzinfo=MANILA)
    assert next_fire_time("30 */5 * * * *", start) == datetime(2026, 11, 3, 8, 10, 30, tzinfo=MANILA)


def test_decorator_registers_job_and_returns_function(scheduler: Scheduler) -> None:
    @scheduler.cron("*/15 * * * *")
    def sync_invoices() -> str:
        return "done"

    [job] = scheduler.jobs
    assert job.name == "sync_invoices"
    assert job.next_run == datetime(2026, 11, 3, 8, 15, tzinfo=MANILA)
    assert sync_invoices() == "done"


def test_runs_only_due_jobs(scheduler: Scheduler, clock: FakeClock) -> None:
    calls: list[str] = []
    scheduler.add_job(lambda: calls.append("fast"), "*/5 * * * *", name="fast")
    scheduler.add_job(lambda: calls.append("hourly"), "0 * * * *", name="hourly")

    assert scheduler.run_pending() == []

    clock.advance(minutes=3)  # 08:10
    assert [job.name for job in scheduler.run_pending()] == ["fast"]

    clock.advance(minutes=50)  # 09:00
    scheduler.run_pending()
    assert calls == ["fast", "fast", "hourly"]


def test_failed_job_is_recorded_and_rescheduled(scheduler: Scheduler, clock: FakeClock) -> None:
    def broken() -> None:
        raise RuntimeError("erp is down")

    job = scheduler.add_job(broken, "*/15 * * * *")
    other = scheduler.add_job(lambda: None, "*/15 * * * *", name="other")

    clock.advance(minutes=8)  # 08:15
    scheduler.run_pending()

    assert (job.runs, job.failures) == (1, 1)
    assert str(job.last_error) == "erp is down"
    assert job.next_run == datetime(2026, 11, 3, 8, 30, tzinfo=MANILA)
    assert other.runs == 1


def test_error_is_cleared_after_a_good_run(scheduler: Scheduler, clock: FakeClock) -> None:
    outcomes = iter([RuntimeError("flaky"), None])

    def flaky() -> None:
        error = next(outcomes)
        if error:
            raise error

    job = scheduler.add_job(flaky, "* * * * *")
    for _ in range(2):
        clock.advance(minutes=1)
        scheduler.run_pending()

    assert job.last_error is None
    assert (job.runs, job.failures) == (2, 1)


def test_missed_slots_are_not_replayed(scheduler: Scheduler, clock: FakeClock) -> None:
    job = scheduler.add_job(lambda: None, "*/5 * * * *", name="sync")

    clock.advance(hours=1)  # missed 12 slots, e.g. the machine was asleep
    scheduler.run_pending()
    scheduler.run_pending()

    assert job.runs == 1
    assert job.next_run > clock()


def test_run_on_start(scheduler: Scheduler) -> None:
    job = scheduler.add_job(lambda: None, "0 0 * * *", name="nightly", run_on_start=True)

    scheduler.run_pending()

    assert job.runs == 1
    assert job.next_run == datetime(2026, 11, 4, 0, 0, tzinfo=MANILA)


def test_rejects_bad_expressions_and_duplicate_names(scheduler: Scheduler) -> None:
    with pytest.raises(ValueError, match="invalid cron expression"):
        scheduler.add_job(lambda: None, "every five minutes", name="bad")

    scheduler.add_job(lambda: None, "* * * * *", name="sync")
    with pytest.raises(ValueError, match="already scheduled"):
        scheduler.add_job(lambda: None, "* * * * *", name="sync")


def test_job_runs_with_its_name_in_the_log_context(scheduler: Scheduler) -> None:
    seen: list[dict[str, object]] = []
    scheduler.add_job(lambda: seen.append(current_log_context()), "* * * * *", name="sync", run_on_start=True)

    scheduler.run_pending()

    assert seen == [{"job": "sync"}]


def test_run_forever_exits_when_stopped() -> None:
    stop = threading.Event()
    scheduler = Scheduler()
    scheduler.add_job(stop.set, "* * * * * *", name="stopper", run_on_start=True)

    thread = threading.Thread(target=scheduler.run_forever, args=(stop,), kwargs={"max_sleep": 0.05})
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert scheduler.jobs[0].runs == 1
