from collections.abc import Hashable
from dataclasses import dataclass

from p2p_sync.sync import KeyMatcher, MatchStatus, RecordMatcher


@dataclass(frozen=True)
class Supplier:
    name: str
    tax_id: str | None


@dataclass(frozen=True)
class Vendor:
    vendor_num: int
    tax_id: str | None


class SupplierVendorMatcher(RecordMatcher[Supplier, Vendor]):
    def source_key(self, source: Supplier) -> Hashable | None:
        return source.tax_id

    def target_key(self, target: Vendor) -> Hashable | None:
        return target.tax_id


def test_classifies_each_source_record() -> None:
    sources = [
        Supplier("Acme Pumps", "11 111"),
        Supplier("New Co", "22 222"),
        Supplier("Twin Ltd", "33 333"),
        Supplier("Keyless", None),
    ]
    targets = [Vendor(1, "11 111"), Vendor(2, "33 333"), Vendor(3, "33 333")]

    matches = SupplierVendorMatcher().match(sources, targets)

    assert [m.status for m in matches] == [
        MatchStatus.MATCHED,
        MatchStatus.UNMATCHED,
        MatchStatus.AMBIGUOUS,
        MatchStatus.NO_KEY,
    ]
    assert matches[0].target == Vendor(1, "11 111")
    assert matches[1].target is None
    assert matches[2].target is None
    assert [v.vendor_num for v in matches[2].candidates] == [2, 3]


def test_preserves_source_order() -> None:
    sources = [Supplier(name, name) for name in ("c", "a", "b")]

    matches = SupplierVendorMatcher().match(sources, [])

    assert [m.source.name for m in matches] == ["c", "a", "b"]


def test_string_keys_are_trimmed_case_folded_and_whitespace_collapsed() -> None:
    matches = SupplierVendorMatcher().match(
        [Supplier("Acme", "  ab  12\tCD ")],
        [Vendor(1, "AB 12 cd")],
    )

    assert matches[0].status is MatchStatus.MATCHED
    assert matches[0].key == "ab 12 cd"


def test_blank_keys_count_as_missing() -> None:
    matches = SupplierVendorMatcher().match([Supplier("Blank", "   ")], [Vendor(1, "   ")])

    assert matches[0].status is MatchStatus.NO_KEY


def test_targets_without_keys_are_never_matched() -> None:
    indexed = SupplierVendorMatcher().index([Vendor(1, None), Vendor(2, "x")])

    assert list(indexed) == ["x"]


def test_normalise_can_be_overridden() -> None:
    class ExactMatcher(SupplierVendorMatcher):
        def normalise(self, key: Hashable | None) -> Hashable | None:
            return key

    matches = ExactMatcher().match([Supplier("Acme", "ABC")], [Vendor(1, "abc")])

    assert matches[0].status is MatchStatus.UNMATCHED


def test_key_matcher_supports_non_string_and_composite_keys() -> None:
    matcher: KeyMatcher[Supplier, Vendor] = KeyMatcher(
        source_key=lambda s: (s.name[0], len(s.name)),
        target_key=lambda v: ("A", v.vendor_num),
    )

    matches = matcher.match([Supplier("Acme", None)], [Vendor(4, None)])

    assert matches[0].status is MatchStatus.MATCHED
    assert matches[0].key == ("A", 4)
