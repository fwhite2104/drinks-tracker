"""Small, deterministic catalog-to-retailer-listing matching workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Protocol

from .collector import BenchmarkPack


@dataclass(frozen=True)
class SourceListing:
    retailer: str
    source_product_reference: str
    source_item_id: str
    name: str
    brand: str | None = None
    variant: str | None = None
    pack_count: int | None = None
    unit_size_ml: int | None = None
    package_type: str | None = None


@dataclass(frozen=True)
class MatchResult:
    status: str  # approved, review, or unmapped
    catalog_id: str | None
    reason: str


_UNIT_RE = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>ml|l)\b", re.I)
_COUNT_RE = re.compile(r"\b(?P<count>\d+)\s*(?:x|×)\s*\d", re.I)
_TRAILING_COUNT_RE = re.compile(r"(?:x|×)\s*(?P<count>\d+)\b", re.I)
_PACK_RE = re.compile(r"\b(?P<count>\d+)\s*(?:pack|pk|cans?|bottles?)\b", re.I)
_GENERIC_WORDS = {"can", "cans", "bottle", "bottles", "pack", "pk", "single", "each"}


class _NamedListing(Protocol):
    @property
    def name(self) -> str: ...


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower().replace("-", " ")))


def _core_tokens(value: str) -> set[str]:
    tokens = _tokens(value)
    tokens -= _GENERIC_WORDS
    tokens = {
        token
        for token in tokens
        if token != "x" and not re.fullmatch(r"x?\d+(?:ml|l)?", token)
    }
    return tokens


def _unit_size_ml(value: str) -> int | None:
    match = _UNIT_RE.search(value)
    if not match:
        return None
    number = float(match.group("value").replace(",", "."))
    return round(number if match.group("unit").lower() == "ml" else number * 1000)


def _pack_count(value: str) -> int:
    match = _COUNT_RE.search(value) or _TRAILING_COUNT_RE.search(value) or _PACK_RE.search(value)
    return int(match.group("count")) if match else 1


def _package_type(value: str) -> str | None:
    tokens = _tokens(value)
    if tokens & {"can", "cans"}:
        return "can"
    if tokens & {"bottle", "bottles"}:
        return "bottle"
    if tokens & {"carton", "cartons"}:
        return "carton"
    return None


def same_text(left: str | None, right: str) -> bool:
    return bool(left) and _core_tokens(left or "") == _core_tokens(right)


def brand_matches_alias(pack: BenchmarkPack, brand: str | None) -> bool:
    """True when *brand* is a curated Brand Alias of the pack's brand.

    Brand Alias (CONTEXT.md): a curated alias translates the retailer's
    consumer brand name (e.g. "Diet Coke") to the catalog's canonical brand
    and variant (Coca-Cola, Diet) before the exact-pack bar is applied. The
    alias belongs to the pack, so it never widens to other variants; the
    variant, pack-count, and unit-size agreement checks still apply.
    """
    if not brand:
        return False
    brand_tokens = _core_tokens(brand)
    return bool(brand_tokens) and any(
        _core_tokens(alias) == brand_tokens for alias in pack.aliases
    )


def _attribute_candidates(
    catalog: Iterable[BenchmarkPack], listing: SourceListing
) -> list[BenchmarkPack]:
    inferred_size = listing.unit_size_ml or _unit_size_ml(listing.name)
    inferred_count = listing.pack_count or _pack_count(listing.name)
    inferred_package = listing.package_type or _package_type(listing.name)
    candidates = []
    for pack in catalog:
        if listing.brand and not (
            same_text(listing.brand, pack.brand)
            or brand_matches_alias(pack, listing.brand)
        ):
            continue
        if listing.variant and not same_text(listing.variant, pack.variant):
            continue
        if inferred_size != pack.unit_size_ml or inferred_count != pack.pack_count:
            continue
        if inferred_package and inferred_package != pack.package_type:
            continue
        candidates.append(pack)
    return candidates


def name_matches(pack: BenchmarkPack, listing: _NamedListing) -> bool:
    source = _core_tokens(listing.name)
    for phrase in (pack.name, *pack.aliases):
        phrase_tokens = _core_tokens(phrase)
        if phrase_tokens and phrase_tokens.issubset(source):
            return True
    return False


def match_catalog(
    catalog: Iterable[BenchmarkPack], listing: SourceListing
) -> MatchResult:
    """Return an approval only for one exact, high-confidence pack match."""
    candidates = _attribute_candidates(catalog, listing)
    if len(candidates) > 1:
        return MatchResult("review", None, "multiple catalog packs share the source attributes")
    if not candidates:
        return MatchResult("unmapped", None, "no catalog pack has the source pack attributes")
    candidate = candidates[0]
    if not name_matches(candidate, listing):
        return MatchResult("unmapped", None, "source name does not match the catalog name or aliases")
    return MatchResult("approved", candidate.catalog_id, "unique exact-pack match")
