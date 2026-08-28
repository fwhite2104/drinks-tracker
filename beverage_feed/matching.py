"""Small, deterministic catalog-to-retailer-listing matching workflow.

Also hosts the curated Brand Alias translation layer (CONTEXT.md: Brand Alias)
and the universal junk relevance gate used by the discovery pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
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
# variant is null when the phrasing carries none). Per CONTRIBUTING.md §10 the
# curated table lives in a data file (data/brand_aliases.json), loaded at call
# time with the same conventions as data/catalog.json. Review sprints extend
# it by editing that file. Auto-mined aliases are suggestions only and never
# enter the dictionary uncurated, so nothing is ever auto-applied at runtime.
BRAND_ALIAS_PATH = Path("data") / "brand_aliases.json"

# Cache of the loaded dictionary keyed by resolved path, invalidated on file
# change (mtime_ns + size) so an edited data file is picked up without a
# process restart and a missing file degrades to an empty table (consistent
# with mappings.json handling).
_ALIAS_CACHE: dict[str, tuple[int, int, dict[str, tuple[str, str | None]]]] = {}


def load_brand_aliases(path: Path = BRAND_ALIAS_PATH) -> dict[str, tuple[str, str | None]]:
    """Load the curated Brand Alias dictionary from *path*.

    Follows the ``load_catalog`` conventions: JSON read from disk, ``ValueError``
    on a malformed shape, and every entry must be a ``[brand, variant]`` pair
    with a null variant when the phrase carries none. A missing file falls back
    to an empty table (matching how a missing mappings file degrades to no
    mappings) rather than raising, so an absent data file narrows matching to
    the pack's own aliases instead of breaking the run.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("brand alias file must contain an object")
    table: dict[str, tuple[str, str | None]] = {}
    for phrase, identity in raw.items():
        if (
            not isinstance(phrase, str)
            or not isinstance(identity, list)
            or len(identity) != 2
            or not isinstance(identity[0], str)
            or not (identity[1] is None or isinstance(identity[1], str))
        ):
            raise ValueError(f"brand alias entry must be [brand, variant|null]: {phrase!r}")
        table[phrase.lower()] = (identity[0], identity[1] or None)
    return table


def _alias_table(path: Path = BRAND_ALIAS_PATH) -> dict[str, tuple[str, str | None]]:
    """Cached :func:`load_brand_aliases`, reloaded when the file changes."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = str(path)
    cached = _ALIAS_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    table = load_brand_aliases(path)
    _ALIAS_CACHE[key] = (stat.st_mtime_ns, stat.st_size, table)
    return table


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
    table = _alias_table()
    for phrase in sorted(table, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", normalized):
            brand, variant = table[phrase]
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
    if brand_tokens:
        for alias in pack.aliases:
            if _core_tokens(alias) != brand_tokens:
                continue
            translation = resolve_brand_alias(alias)
            if translation is None:
                # Pack-curated alias with no dictionary reading: the alias is
                # itself the curated identity evidence.
                return True
            # The alias's own translation must agree with the pack's canonical
            # identity: a wrong or cross-variant curated alias can never
            # approve (CONTEXT.md: Brand Alias never weakens the bar).
            if not same_text(pack.brand, translation.brand):
                continue
            if translation.variant is not None and not same_text(pack.variant, translation.variant):
                continue
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
    translation = resolve_brand_alias(listing.brand) if listing.brand else None
    # Variant evidence: the listing's own variant, else the variant implied by
    # its curated brand alias. A stated brand with no variant can never
    # silently widen the match by size alone: the alias-implied variant must
    # pin the pack's variant, and a pack that declares a variant requires
    # agreeing evidence.
    variant_evidence = listing.variant or (translation.variant if translation else None)
    candidates = []
    for pack in catalog:
        if listing.brand and not (
            same_text(listing.brand, pack.brand)
            or brand_matches_alias(pack, listing.brand)
        ):
            continue
        if listing.variant is not None:
            if not same_text(listing.variant, pack.variant):
                continue
        elif listing.brand and pack.variant and not same_text(variant_evidence, pack.variant):
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
