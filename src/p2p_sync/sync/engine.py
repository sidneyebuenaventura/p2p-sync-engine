"""One-way sync from a source system into a target system.

Unmatched records get created, matched ones get updated (or skipped), and anything
ambiguous is left alone as a conflict. Errors are kept per record so one bad invoice
doesn't kill the whole batch.

We always match before writing, so a rerun is safe: if a record was created but
finalize failed, the next run finds it and skips it.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from p2p_sync.sync.matcher import Match, MatchStatus, RecordMatcher

Stage = Literal["create", "update", "finalize"]


class SyncAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    SKIP = "skip"
    CONFLICT = "conflict"


@dataclass(slots=True)
class SyncResult[S, T]:
    source: S
    action: SyncAction
    target: T | None = None
    reason: str | None = None
    error: Exception | None = None
    failed_stage: Stage | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.action is not SyncAction.CONFLICT


@dataclass(slots=True)
class SyncReport[S, T]:
    results: list[SyncResult[S, T]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def failures(self) -> list[SyncResult[S, T]]:
        return [r for r in self.results if r.error is not None]

    @property
    def conflicts(self) -> list[SyncResult[S, T]]:
        return [r for r in self.results if r.action is SyncAction.CONFLICT]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def counts(self) -> dict[str, int]:
        counter = Counter(r.action.value for r in self.results if r.error is None)
        counts = {action.value: counter.get(action.value, 0) for action in SyncAction}
        counts["failed"] = len(self.failures)
        return counts

    def summary(self) -> str:
        prefix = "[dry run] " if self.dry_run else ""
        return prefix + " ".join(f"{name}={count}" for name, count in self.counts().items())


class SyncEngine[S, T]:
    def __init__(
        self,
        matcher: RecordMatcher[S, T],
        *,
        create: Callable[[S], T],
        update: Callable[[S, T], T] | None = None,
        needs_update: Callable[[S, T], bool] | None = None,
        finalize: Callable[[SyncResult[S, T]], None] | None = None,
        report_problem: Callable[[SyncResult[S, T]], None] | None = None,
        dry_run: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        # update=None means "insert only": matched records are skipped.
        # finalize runs after a successful write, report_problem gets conflicts and failures.
        self.matcher = matcher
        self._create = create
        self._update = update
        self._needs_update = needs_update or (lambda _source, _target: True)
        self._finalize = finalize
        self._report_problem = report_problem
        self.dry_run = dry_run
        self.logger = logger or logging.getLogger(__name__)

    def run(self, sources: Iterable[S], targets: Iterable[T]) -> SyncReport[S, T]:
        report: SyncReport[S, T] = SyncReport(dry_run=self.dry_run)
        for match in self.matcher.match(sources, targets):
            result = self._sync_one(match)
            report.results.append(result)
            self._log(result)
        self.logger.info("sync finished: %s", report.summary())
        return report

    def _sync_one(self, match: Match[S, T]) -> SyncResult[S, T]:
        result = self._plan(match)
        if self.dry_run:
            return result

        if result.action is SyncAction.CREATE:
            self._attempt(result, "create", lambda: self._create(match.source))
        elif result.action is SyncAction.UPDATE and self._update is not None and result.target is not None:
            update, current = self._update, result.target
            self._attempt(result, "update", lambda: update(match.source, current))

        if result.action in (SyncAction.CREATE, SyncAction.UPDATE) and result.error is None:
            self._run_finalize(result)

        if not result.ok:
            self._run_report_problem(result)
        return result

    def _plan(self, match: Match[S, T]) -> SyncResult[S, T]:
        source = match.source
        match match.status:
            case MatchStatus.UNMATCHED:
                return SyncResult(source, SyncAction.CREATE)
            case MatchStatus.MATCHED:
                target = match.candidates[0]  # MATCHED means exactly one candidate
                if self._update is None:
                    return SyncResult(source, SyncAction.SKIP, target, reason="already exists")
                if not self._needs_update(source, target):
                    return SyncResult(source, SyncAction.SKIP, target, reason="up to date")
                return SyncResult(source, SyncAction.UPDATE, target)
            case MatchStatus.AMBIGUOUS:
                reason = f"{len(match.candidates)} target records share key {match.key!r}"
                return SyncResult(source, SyncAction.CONFLICT, reason=reason)
            case MatchStatus.NO_KEY:
                return SyncResult(source, SyncAction.CONFLICT, reason="source record has no matching key")

    def _attempt(self, result: SyncResult[S, T], stage: Stage, write: Callable[[], T]) -> None:
        try:
            result.target = write()
        except Exception as exc:
            result.error, result.failed_stage = exc, stage

    def _run_finalize(self, result: SyncResult[S, T]) -> None:
        if self._finalize is None:
            return
        try:
            self._finalize(result)
        except Exception as exc:
            result.error, result.failed_stage = exc, "finalize"

    def _run_report_problem(self, result: SyncResult[S, T]) -> None:
        if self._report_problem is None:
            return
        try:
            self._report_problem(result)
        except Exception:
            # best effort - don't let a broken status call hide the real error
            self.logger.exception("report_problem failed for %r", result.source)

    def _log(self, result: SyncResult[S, T]) -> None:
        if result.error is not None:
            self.logger.error("%s failed during %s: %s", result.source, result.failed_stage, result.error)
        elif result.action is SyncAction.CONFLICT:
            self.logger.warning("%s conflict: %s", result.source, result.reason)
        else:
            self.logger.info("%s -> %s", result.source, result.action.value)
