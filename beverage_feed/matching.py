"""Small, deterministic catalog-to-retailer-listing matching workflow.

Also hosts the curated Brand Alias translation layer (CONTEXT.md: Brand Alias)
and the universal junk relevance gate used by the discovery pipeline.
"""

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


# Curated Brand Alias dictionary (CONTEXT.md: Brand Alias). Maps a retailer's
# consumer brand phrasing to the catalog's canonical brand and variant (the
# variant is None when the phrasing carries none). Starts with the catalog's
# top brands; review sprints extend it by editing this table. Auto-mined
# aliases are suggestions only and never enter the dictionary uncurated, so
# nothing is ever auto-applied at runtime.
BRAND_ALIAS_DICTIONARY: dict[str, tuple[str, str | None]] = {
    # Coke family
    "diet coke": ("Coca-Cola", "Diet"),
    "coke diet": ("Coca-Cola", "Diet"),
    "coke zero": ("Coca-Cola", "Zero Sugar"),
    "coke original": ("Coca-Cola", "Original Taste"),
    "coke original taste": ("Coca-Cola", "Original Taste"),
    "coca cola zero": ("Coca-Cola", "Zero Sugar"),
    "coke": ("Coca-Cola", None),
    "coca cola": ("Coca-Cola", None),
    # Other top brands
    "7up free": ("7UP", "Free"),
    "7up": ("7UP", None),
    "fanta zero": ("Fanta", "Orange Zero"),
    "fanta": ("Fanta", None),
    "sprite zero": ("Sprite", "Zero Sugar"),
    "sprite": ("Sprite", None),
    "lucozade sport": ("Lucozade Sport", None),
    "lucozade": ("Lucozade", None),
    "rock original": ("Rockstar", "Original"),
    "rock": ("Rockstar", None),
    "pepsi max": ("Pepsi", "Max"),
}

_ALIAS_PHRASES = tuple(sorted(BRAND_ALIAS_DICTIONARY, key=len, reverse=True))


@dataclass(frozen=True)
class BrandAliasTranslation:
    """One curated alias resolution: phrase found, canonical identity."""

    phrase: str
    brand: str
    variant: str | None


def resolve_brand_alias(text: str | None) -> BrandAliasTranslation | None:
    """Translate a curated Brand Alias phrase inside *text*, longest first.

    Matching is token-based and case/hyphen insensitive ("Diet-Coke" equals
    "diet coke"). Returns None when no curated phrase occurs, so junk names
    ("POWCUT hoodie", "LED lamp") never gain brand identity.
    """
    if not text:
        return None
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("-", " ")))
    for phrase in _ALIAS_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", normalized):
            brand, variant = BRAND_ALIAS_DICTIONARY[phrase]
            return BrandAliasTranslation(phrase, brand, variant)
    return None


def brand_matches_alias(pack: BenchmarkPack, brand: str | None) -> bool:
    """True when *brand* is a curated Brand Alias of the pack's brand.

    Brand Alias (CONTEXT.md): a curated alias translates the retailer's
    consumer brand name (e.g. "Diet Coke") to the catalog's canonical brand
    and variant (Coca-Cola, Diet) before the exact-pack bar is applied. Two
    curated sources feed this check: the pack's own aliases and the shared
    Brand Alias dictionary. The alias belongs to the pack, so it never widens
    to other variants; the variant, pack-count, and unit-size agreement
    checks still apply.
    """
    if not brand:
        return False
    brand_tokens = _core_tokens(brand)
    if brand_tokens and any(
        _core_tokens(alias) == brand_tokens for alias in pack.aliases
    ):
        return True
    translation = resolve_brand_alias(brand)
    if translation is None or not same_text(pack.brand, translation.brand):
        return False
    return translation.variant is None or same_text(pack.variant, translation.variant)


def identity_phrases(pack: BenchmarkPack) -> tuple[str, ...]:
    """Every phrase that carries the pack's brand/identity tokens."""
    return (pack.search_term, pack.name, pack.brand, pack.variant, *pack.aliases)


def shares_identity_token(name: str, phrases: Iterable[str]) -> bool:
    """True when *name* shares at least one core token with any phrase.

    Universal junk gate: a candidate attaches to a catalog cell only when its
    name shares a brand/identity token with the search term or pack. Size and
    count tokens are excluded (a 330ml lamp is still a lamp), so POWERCUT
    clothing and LED lamps never attach to drink cells.
    """
    tokens = _core_tokens(name)
    if not tokens:
        return False
    return any(tokens & _core_tokens(phrase) for phrase in phrases if phrase)


def is_relevant_candidate(name: str, pack: BenchmarkPack) -> bool:
    """Junk gate against the pack's full identity: term, name, brand, variant."""
    return shares_identity_token(name, identity_phrases(pack))


def attribute_candidates(
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


def search_formulations(pack: BenchmarkPack) -> tuple[str, ...]:
    """Alternate search formulations for one catalog pack (ticket 14).

    Ordered, unique: the pack's search term, its curated aliases
    (alias-explicit, e.g. "Diet Coke"), then count-explicit
    ("<identity> 8 pack") and size-explicit ("<identity> 330ml" /
    "<identity> 2 litre") phrasings built from the canonical brand+variant
    identity and each alias.  Retailer searches answer different phrasings
    with different result sets, so the re-discovery pass over thin and
    Class-D cells searches these alternates instead of re-issuing the
    original query.
    """
    canonical = " ".join(part for part in (pack.brand, pack.variant) if part)
    heads: list[str] = []
    for phrase in (canonical, *pack.aliases):
        stripped = phrase.strip()
        if stripped and stripped not in heads:
            heads.append(stripped)
    terms: list[str] = []
    for phrase in (pack.search_term, *pack.aliases):
        stripped = phrase.strip()
        if stripped and stripped not in terms:
            terms.append(stripped)
    if pack.pack_count > 1:
        for head in heads:
            terms.append(f"{head} {pack.pack_count} pack")
    for head in heads:
        size = (
            f"{pack.unit_size_ml // 1000} litre"
            if pack.unit_size_ml % 1000 == 0
            else f"{pack.unit_size_ml}ml"
        )
        terms.append(f"{head} {size}")
    return tuple(dict.fromkeys(terms))


def match_catalog(
    catalog: Iterable[BenchmarkPack], listing: SourceListing
) -> MatchResult:
    """Return an approval only for one exact, high-confidence pack match."""
    candidates = attribute_candidates(catalog, listing)
    if len(candidates) > 1:
        return MatchResult("review", None, "multiple catalog packs share the source attributes")
    if not candidates:
        return MatchResult("unmapped", None, "no catalog pack has the source pack attributes")
    candidate = candidates[0]
    if not name_matches(candidate, listing):
        return MatchResult("unmapped", None, "source name does not match the catalog name or aliases")
    return MatchResult("approved", candidate.catalog_id, "unique exact-pack match")
