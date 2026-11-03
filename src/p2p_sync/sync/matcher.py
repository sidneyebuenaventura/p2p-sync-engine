"""Match source records to target records by a key.

Subclasses implement source_key() and target_key(). String keys are trimmed and
case-folded before comparing, since the same supplier code is rarely typed the same
way in two systems.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class MatchStatus(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"  # more than one target with the same key
    NO_KEY = "no_key"


@dataclass(frozen=True, slots=True)
class Match[S, T]:
    source: S
    status: MatchStatus
    key: Hashable | None = None
    candidates: Sequence[T] = field(default_factory=tuple)

    @property
    def target(self) -> T | None:
        return self.candidates[0] if self.status is MatchStatus.MATCHED else None


class RecordMatcher[S, T](ABC):
    @abstractmethod
    def source_key(self, source: S) -> Hashable | None: ...

    @abstractmethod
    def target_key(self, target: T) -> Hashable | None: ...

    def normalise(self, key: Hashable | None) -> Hashable | None:
        if isinstance(key, str):
            key = " ".join(key.split()).casefold()
            return key or None
        return key

    def index(self, targets: Iterable[T]) -> dict[Hashable, list[T]]:
        indexed: dict[Hashable, list[T]] = defaultdict(list)
        for target in targets:
            key = self.normalise(self.target_key(target))
            if key is not None:
                indexed[key].append(target)
        return indexed

    def match(self, sources: Iterable[S], targets: Iterable[T]) -> list[Match[S, T]]:
        indexed = self.index(targets)
        matches: list[Match[S, T]] = []
        for source in sources:
            key = self.normalise(self.source_key(source))
            if key is None:
                matches.append(Match(source, MatchStatus.NO_KEY))
                continue
            candidates = tuple(indexed.get(key, ()))
            if not candidates:
                status = MatchStatus.UNMATCHED
            elif len(candidates) == 1:
                status = MatchStatus.MATCHED
            else:
                status = MatchStatus.AMBIGUOUS
            matches.append(Match(source, status, key, candidates))
        return matches


class KeyMatcher[S, T](RecordMatcher[S, T]):
    """Matcher from two plain functions, for when a subclass is overkill."""

    def __init__(
        self,
        source_key: Callable[[S], Hashable | None],
        target_key: Callable[[T], Hashable | None],
    ) -> None:
        self._source_key = source_key
        self._target_key = target_key

    def source_key(self, source: S) -> Hashable | None:
        return self._source_key(source)

    def target_key(self, target: T) -> Hashable | None:
        return self._target_key(target)
