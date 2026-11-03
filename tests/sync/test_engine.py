from dataclasses import dataclass, replace

import pytest

from p2p_sync.sync import KeyMatcher
from p2p_sync.sync.engine import SyncAction, SyncEngine, SyncResult


@dataclass(frozen=True)
class Invoice:
    number: str | None
    amount: int


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: int
    invoice_number: str
    amount: int


def matcher() -> KeyMatcher[Invoice, LedgerEntry]:
    return KeyMatcher(lambda inv: inv.number, lambda entry: entry.invoice_number)


class FakeLedger:
    """In-memory target system that records every write."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.entries: list[LedgerEntry] = []
        self.fail_on = fail_on or set()
        self.writes: list[str] = []

    def create(self, invoice: Invoice) -> LedgerEntry:
        assert invoice.number is not None
        if invoice.number in self.fail_on:
            raise RuntimeError(f"ledger rejected {invoice.number}")
        entry = LedgerEntry(len(self.entries) + 100, invoice.number, invoice.amount)
        self.entries.append(entry)
        self.writes.append(f"create {invoice.number}")
        return entry

    def update(self, invoice: Invoice, entry: LedgerEntry) -> LedgerEntry:
        if invoice.number in self.fail_on:
            raise RuntimeError(f"ledger rejected {invoice.number}")
        self.writes.append(f"update {invoice.number}")
        return replace(entry, amount=invoice.amount)


def test_creates_updates_skips_and_flags_conflicts() -> None:
    ledger = FakeLedger()
    existing = [
        LedgerEntry(1, "INV-2", 50),  # amount changed -> update
        LedgerEntry(2, "INV-3", 70),  # unchanged -> skip
        LedgerEntry(3, "INV-4", 10),  # duplicated key -> conflict
        LedgerEntry(4, "INV-4", 10),
    ]
    sources = [
        Invoice("INV-1", 10),
        Invoice("INV-2", 55),
        Invoice("INV-3", 70),
        Invoice("INV-4", 10),
        Invoice(None, 5),
    ]
    engine = SyncEngine(
        matcher(),
        create=ledger.create,
        update=ledger.update,
        needs_update=lambda inv, entry: inv.amount != entry.amount,
    )

    report = engine.run(sources, existing)

    assert [r.action for r in report.results] == [
        SyncAction.CREATE,
        SyncAction.UPDATE,
        SyncAction.SKIP,
        SyncAction.CONFLICT,
        SyncAction.CONFLICT,
    ]
    assert ledger.writes == ["create INV-1", "update INV-2"]
    assert report.results[1].target == LedgerEntry(1, "INV-2", 55)
    assert report.results[2].reason == "up to date"
    assert report.results[3].reason == "2 target records share key 'inv-4'"
    assert report.counts() == {"create": 1, "update": 1, "skip": 1, "conflict": 2, "failed": 0}
    assert not report.ok


def test_without_update_matches_are_skipped() -> None:
    ledger = FakeLedger()
    engine = SyncEngine(matcher(), create=ledger.create)

    report = engine.run([Invoice("INV-1", 1)], [LedgerEntry(1, "INV-1", 999)])

    assert report.results[0].action is SyncAction.SKIP
    assert report.results[0].reason == "already exists"
    assert ledger.writes == []


def test_one_failure_does_not_stop_the_batch() -> None:
    ledger = FakeLedger(fail_on={"INV-2"})
    problems: list[SyncResult[Invoice, LedgerEntry]] = []
    engine = SyncEngine(matcher(), create=ledger.create, report_problem=problems.append)

    report = engine.run([Invoice("INV-1", 1), Invoice("INV-2", 2), Invoice("INV-3", 3)], [])

    assert ledger.writes == ["create INV-1", "create INV-3"]
    [failure] = report.failures
    assert failure.source.number == "INV-2"
    assert failure.failed_stage == "create"
    assert str(failure.error) == "ledger rejected INV-2"
    assert problems == [failure]
    assert report.counts()["failed"] == 1
    assert report.counts()["create"] == 2


def test_finalize_runs_after_successful_writes_only() -> None:
    finalized: list[str | None] = []
    ledger = FakeLedger(fail_on={"INV-2"})
    engine = SyncEngine(
        matcher(),
        create=ledger.create,
        finalize=lambda result: finalized.append(result.source.number),
    )

    engine.run([Invoice("INV-1", 1), Invoice("INV-2", 2), Invoice(None, 3)], [])

    assert finalized == ["INV-1"]


def test_finalize_failure_is_recorded_and_reported() -> None:
    problems: list[SyncResult[Invoice, LedgerEntry]] = []

    def finalize(_result: SyncResult[Invoice, LedgerEntry]) -> None:
        raise ConnectionError("could not mark exported")

    engine = SyncEngine(
        matcher(), create=FakeLedger().create, finalize=finalize, report_problem=problems.append
    )

    [result] = engine.run([Invoice("INV-1", 1)], []).results

    assert result.action is SyncAction.CREATE
    assert result.target is not None  # the write itself succeeded
    assert result.failed_stage == "finalize"
    assert problems == [result]


def test_failing_report_problem_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    def report_problem(_result: SyncResult[Invoice, LedgerEntry]) -> None:
        raise RuntimeError("status endpoint down")

    engine = SyncEngine(matcher(), create=FakeLedger().create, report_problem=report_problem)

    report = engine.run([Invoice(None, 1)], [])

    assert report.conflicts
    assert "report_problem failed" in caplog.text


def test_dry_run_plans_without_side_effects() -> None:
    ledger = FakeLedger()
    side_effects: list[str] = []
    engine = SyncEngine(
        matcher(),
        create=ledger.create,
        update=ledger.update,
        finalize=lambda _r: side_effects.append("finalize"),
        report_problem=lambda _r: side_effects.append("problem"),
        dry_run=True,
    )

    report = engine.run(
        [Invoice("INV-1", 1), Invoice("INV-2", 2), Invoice(None, 3)],
        [LedgerEntry(1, "INV-2", 0)],
    )

    assert [r.action for r in report.results] == [SyncAction.CREATE, SyncAction.UPDATE, SyncAction.CONFLICT]
    assert ledger.writes == []
    assert side_effects == []
    assert report.summary() == "[dry run] create=1 update=1 skip=0 conflict=1 failed=0"


def test_rerun_after_success_is_idempotent() -> None:
    ledger = FakeLedger()
    engine = SyncEngine(matcher(), create=ledger.create)
    sources = [Invoice("INV-1", 1), Invoice("INV-2", 2)]

    engine.run(sources, ledger.entries)
    second = engine.run(sources, ledger.entries)

    assert ledger.writes == ["create INV-1", "create INV-2"]
    assert second.counts()["skip"] == 2
    assert second.ok
