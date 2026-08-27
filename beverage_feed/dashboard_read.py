"""Read-only dashboard data seam over JSON + SQLite.

JSON catalog/mappings/rejections are authoritative for identity and approval
state. SQLite (opened read-only, never created or migrated) is authoritative
for Price Observations, Collection Results/Runs, and discovery operational
state. Writers remain the CLI modules only.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

from .collector import BenchmarkPack, load_catalog
from .discovery import load_mappings, load_rejections

# Observation projection mirrored from collector._OBSERVATION_COLUMNS so the
# dashboard can share the Current Feed / Last Seen semantics without calling
# collector helpers that open SQLite read-write and run ensure_schema.
_OBSERVATION_COLUMNS = """
    po.run_id,
    po.catalog_id,
    cp.name AS catalog_name,
    po.retailer,
    po.source_product_reference,
    po.source_item_id,
    po.source_product_name,
    po.displayed_price,
    po.clubcard_price,
    po.drs_deposit,
    po.source_scope,
    po.currency,
    po.pack_count,
    po.unit_size_ml,
    po.package_type,
    po.component_unit_price,
    po.price_per_litre,
    po.observed_at
"""

# Tier-1 registry shared with collector seed knowledge (not invented in the UI).
SUPPORTED_RETAILERS: tuple[dict[str, Any], ...] = (
    {"slug": "tesco", "display_name": "Tesco Ireland", "tier": 1},
    {"slug": "dunnes", "display_name": "Dunnes Stores", "tier": 1},
    {"slug": "supervalu", "display_name": "SuperValu", "tier": 1},
    {"slug": "lidl", "display_name": "Lidl Ireland", "tier": 1},
    {"slug": "aldi", "display_name": "Aldi Ireland", "tier": 1},
)

RETAILER_SLUGS: tuple[str, ...] = tuple(row["slug"] for row in SUPPORTED_RETAILERS)
_RETAILER_NAMES = {row["slug"]: row["display_name"] for row in SUPPORTED_RETAILERS}

_DEFAULT_DATABASE = "data/feed.sqlite"
_MONEY = Decimal("0.01")


@dataclass(frozen=True)
class DatabaseInfo:
    """Resolved SQLite path and openability (never created by the dashboard)."""

    path: Path
    exists: bool
    openable: bool
    error: str | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Filesystem + DB snapshot at load time; pure relative to paths."""

    repo_root: Path
    catalog_path: Path
    mappings_path: Path
    rejections_path: Path
    catalog: tuple[BenchmarkPack, ...]
    mappings: dict[str, list[dict[str, Any]]]
    rejections: dict[str, list[dict[str, Any]]]
    database: DatabaseInfo
    retailers: tuple[dict[str, Any], ...]
    workspace_state: str  # no_database | no_run | partial_run


def resolve_repo_root(start: Path | None = None) -> Path:
    """Walk upward from *start* (or cwd) until catalog.json is found."""
    here = (start or Path.cwd()).resolve()
    candidates = [here, *here.parents]
    for candidate in candidates:
        if (candidate / "data" / "catalog.json").is_file():
            return candidate
        if (candidate / "beverage_feed").is_dir() and (candidate / "data" / "catalog.json").is_file():
            return candidate
    raise FileNotFoundError(
        "cannot resolve repository root: data/catalog.json not found "
        f"above {here}"
    )


def default_database_path(repo_root: Path) -> Path:
    env = os.environ.get("DRINKS_DATABASE")
    if env:
        path = Path(env)
        return path if path.is_absolute() else (repo_root / path).resolve()
    return (repo_root / _DEFAULT_DATABASE).resolve()


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite database read-only; never create or migrate."""
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _probe_database(path: Path) -> DatabaseInfo:
    if not path.exists():
        return DatabaseInfo(path=path, exists=False, openable=False, error=None)
    try:
        # Reject non-SQLite files: mode=ro still "opens" some garbage as empty DBs.
        header = path.read_bytes()[:16]
        if header and not header.startswith(b"SQLite format 3"):
            return DatabaseInfo(
                path=path,
                exists=True,
                openable=False,
                error="not a SQLite database",
            )
        with closing(_open_readonly(path)) as connection:
            connection.execute("SELECT 1").fetchone()
        return DatabaseInfo(path=path, exists=True, openable=True, error=None)
    except (OSError, sqlite3.Error) as exc:
        return DatabaseInfo(path=path, exists=True, openable=False, error=str(exc))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _workspace_state(database: DatabaseInfo) -> str:
    if not database.exists or not database.openable:
        return "no_database"
    try:
        with closing(_open_readonly(database.path)) as connection:
            if not _table_exists(connection, "collection_runs"):
                return "no_run"
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM collection_runs"
            ).fetchone()["n"]
            if count == 0:
                return "no_run"
            return "partial_run"
    except sqlite3.Error:
        return "no_database"


def load_workspace(
    repo_root: Path | str | None = None,
    database_path: Path | str | None = None,
    *,
    catalog_path: Path | str | None = None,
    mappings_path: Path | str | None = None,
    rejections_path: Path | str | None = None,
) -> WorkspaceSnapshot:
    """Load JSON identity + optional read-only SQLite operational state."""
    root = Path(repo_root).resolve() if repo_root else resolve_repo_root()
    catalog_file = Path(catalog_path) if catalog_path else root / "data" / "catalog.json"
    mappings_file = Path(mappings_path) if mappings_path else root / "data" / "mappings.json"
    rejections_file = (
        Path(rejections_path) if rejections_path else root / "data" / "rejections.json"
    )
    db_path = (
        Path(database_path).resolve()
        if database_path
        else default_database_path(root)
    )

    catalog = tuple(load_catalog(catalog_file))
    mappings = load_mappings(mappings_file) if mappings_file.exists() else {}
    rejections = (
        load_rejections(rejections_file)
        if rejections_file.exists()
        else {"listings": [], "cells": []}
    )
    database = _probe_database(db_path)
    return WorkspaceSnapshot(
        repo_root=root,
        catalog_path=catalog_file.resolve(),
        mappings_path=mappings_file.resolve(),
        rejections_path=rejections_file.resolve(),
        catalog=catalog,
        mappings=mappings,
        rejections=rejections,
        database=database,
        retailers=SUPPORTED_RETAILERS,
        workspace_state=_workspace_state(database),
    )


def _approved_mapping_index(
    mappings: Mapping[str, list[dict[str, Any]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Index approved (non-dormant) JSON mappings by (retailer, catalog_id)."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for retailer, rows in mappings.items():
        for row in rows:
            status = row.get("status") or "approved"
            if status == "dormant":
                continue
            if status != "approved":
                continue
            catalog_id = row.get("catalog_id")
            if not isinstance(catalog_id, str) or not catalog_id:
                continue
            index[(retailer, catalog_id)] = row
    return index


def _rejected_cells(
    rejections: Mapping[str, list[dict[str, Any]]],
) -> set[tuple[str, str]]:
    cells: set[tuple[str, str]] = set()
    for row in rejections.get("cells") or []:
        if row.get("state") != "do_not_map":
            continue
        catalog_id = row.get("cell") or row.get("catalog_id")
        retailer = row.get("retailer")
        if isinstance(catalog_id, str) and isinstance(retailer, str):
            cells.add((retailer, catalog_id))
    return cells


def _observation_count(snapshot: WorkspaceSnapshot) -> int:
    if not snapshot.database.openable:
        return 0
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "price_observations"):
                return 0
            return int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM price_observations"
                ).fetchone()["n"]
            )
    except sqlite3.Error:
        return 0


def overview_stats(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    """Summary counts for the Overview strip."""
    approved = _approved_mapping_index(snapshot.mappings)
    return {
        "catalog_packs": len(snapshot.catalog),
        "approved_mappings": len(approved),
        "supported_retailers": len(snapshot.retailers),
        "observation_count": _observation_count(snapshot),
        "workspace_state": snapshot.workspace_state,
        "database": {
            "path": str(snapshot.database.path),
            "exists": snapshot.database.exists,
            "openable": snapshot.database.openable,
            "error": snapshot.database.error,
        },
    }


def _latest_runs_by_retailer(snapshot: WorkspaceSnapshot) -> dict[str, dict[str, Any]]:
    """Latest collection run that touched each retailer (from results)."""
    if not snapshot.database.openable:
        return {}
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "collection_results"):
                return {}
            if not _table_exists(connection, "collection_runs"):
                return {}
            rows = connection.execute(
                """
                SELECT cr.retailer,
                       cr.run_id,
                       run.started_at,
                       run.finished_at,
                       run.status AS run_status,
                       run.observed_count,
                       run.failed_count,
                       SUM(CASE WHEN cr.status = 'observed' THEN 1 ELSE 0 END) AS observed,
                       SUM(CASE WHEN cr.status = 'not_found' THEN 1 ELSE 0 END) AS not_found,
                       SUM(CASE WHEN cr.status = 'source_error' THEN 1 ELSE 0 END) AS source_error,
                       SUM(CASE WHEN cr.status = 'unmapped' THEN 1 ELSE 0 END) AS unmapped,
                       COUNT(*) AS result_count
                FROM collection_results AS cr
                JOIN collection_runs AS run ON run.run_id = cr.run_id
                JOIN (
                    SELECT retailer, MAX(recorded_at) AS latest_at
                    FROM collection_results
                    GROUP BY retailer
                ) AS latest
                  ON latest.retailer = cr.retailer
                 AND cr.recorded_at = latest.latest_at
                GROUP BY cr.retailer, cr.run_id
                """
            ).fetchall()
            # Prefer the run with the most recent finished_at if multiple share timestamp.
            by_retailer: dict[str, dict[str, Any]] = {}
            for row in rows:
                payload = {
                    "retailer": row["retailer"],
                    "run_id": row["run_id"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "run_status": row["run_status"],
                    "observed_count": row["observed_count"],
                    "failed_count": row["failed_count"],
                    "observed": row["observed"],
                    "not_found": row["not_found"],
                    "source_error": row["source_error"],
                    "unmapped": row["unmapped"],
                    "result_count": row["result_count"],
                    "state": "collected",
                }
                existing = by_retailer.get(row["retailer"])
                if existing is None or (row["finished_at"] or "") > (
                    existing.get("finished_at") or ""
                ):
                    by_retailer[row["retailer"]] = payload
            return by_retailer
    except sqlite3.Error:
        return {}


def collection_health(snapshot: WorkspaceSnapshot) -> list[dict[str, Any]]:
    """Per-retailer latest run summary, or Not collected."""
    latest = _latest_runs_by_retailer(snapshot)
    rows: list[dict[str, Any]] = []
    for retailer in SUPPORTED_RETAILERS:
        slug = retailer["slug"]
        entry = latest.get(slug)
        if entry is None:
            rows.append(
                {
                    "retailer": slug,
                    "display_name": retailer["display_name"],
                    "state": "not_collected",
                    "label": "Not collected",
                    "run_id": None,
                    "finished_at": None,
                    "observed": 0,
                    "not_found": 0,
                    "source_error": 0,
                    "unmapped": 0,
                    "result_count": 0,
                }
            )
        else:
            rows.append(
                {
                    **entry,
                    "display_name": retailer["display_name"],
                    "label": "Collected",
                }
            )
    return rows


def _discovery_table_counts(
    snapshot: WorkspaceSnapshot,
) -> dict[str, Any] | None:
    if not snapshot.database.openable:
        return None
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "discovery_cells"):
                return None
            run_count = 0
            if _table_exists(connection, "discovery_runs"):
                run_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM discovery_runs"
                    ).fetchone()["n"]
                )
            if run_count == 0:
                # Table may exist from a partial open elsewhere; treat as no run.
                cell_count = int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM discovery_cells"
                    ).fetchone()["n"]
                )
                if cell_count == 0:
                    return None
            per_retailer: dict[str, dict[str, int]] = {
                slug: {
                    "approved": 0,
                    "review": 0,
                    "unmapped": 0,
                    "pending": 0,
                    "do_not_map": 0,
                    "rejected": 0,
                    "challenge": 0,
                    "other": 0,
                }
                for slug in RETAILER_SLUGS
            }
            for row in connection.execute(
                "SELECT retailer, state, review_category FROM discovery_cells"
            ):
                bucket = per_retailer.get(row["retailer"])
                if bucket is None:
                    continue
                state = row["state"] or ""
                if state == "review" and row["review_category"] == "challenge":
                    bucket["challenge"] += 1
                    bucket["review"] += 1
                elif state in bucket:
                    bucket[state] += 1
                else:
                    bucket["other"] += 1
            return {
                "state": "available",
                "discovery_runs": run_count,
                "per_retailer": [
                    {"retailer": slug, "display_name": _RETAILER_NAMES[slug], **counts}
                    for slug, counts in per_retailer.items()
                ],
            }
    except sqlite3.Error:
        return None


def discovery_summary(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    """Coverage-shaped discovery summary, or a truthful empty state."""
    counts = _discovery_table_counts(snapshot)
    if counts is None:
        return {
            "state": "no_discovery_run",
            "label": "No discovery run yet",
            "discovery_runs": 0,
            "per_retailer": [],
        }
    return counts


def _dormant_cells(snapshot: WorkspaceSnapshot) -> set[tuple[str, str]]:
    if not snapshot.database.openable:
        return set()
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "catalog_mappings"):
                return set()
            return {
                (row["retailer"], row["catalog_id"])
                for row in connection.execute(
                    "SELECT retailer, catalog_id FROM catalog_mappings "
                    "WHERE status = 'dormant'"
                )
            }
    except sqlite3.Error:
        return set()


def _latest_collection_results(
    snapshot: WorkspaceSnapshot,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest Collection Result per (retailer, catalog_id)."""
    if not snapshot.database.openable:
        return {}
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "collection_results"):
                return {}
            rows = connection.execute(
                """
                SELECT retailer, catalog_id, status, error, source_scope,
                       recorded_at, run_id
                FROM collection_results AS cr
                WHERE rowid = (
                    SELECT cr2.rowid FROM collection_results AS cr2
                    WHERE cr2.retailer = cr.retailer
                      AND cr2.catalog_id = cr.catalog_id
                    ORDER BY cr2.recorded_at DESC, cr2.rowid DESC
                    LIMIT 1
                )
                """
            ).fetchall()
            return {
                (row["retailer"], row["catalog_id"]): dict(row) for row in rows
            }
    except sqlite3.Error:
        return {}


def _discovery_cell_states(
    snapshot: WorkspaceSnapshot,
) -> dict[tuple[str, str], dict[str, Any]]:
    if not snapshot.database.openable:
        return {}
    try:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            if not _table_exists(connection, "discovery_cells"):
                return {}
            return {
                (row["retailer"], row["catalog_id"]): {
                    "state": row["state"],
                    "review_category": row["review_category"],
                }
                for row in connection.execute(
                    "SELECT retailer, catalog_id, state, review_category "
                    "FROM discovery_cells"
                )
            }
    except sqlite3.Error:
        return {}


def _admin_mapping_state(
    *,
    retailer: str,
    catalog_id: str,
    approved: Mapping[tuple[str, str], dict[str, Any]],
    rejected: set[tuple[str, str]],
    dormant: set[tuple[str, str]],
    discovery: Mapping[tuple[str, str], dict[str, Any]],
) -> str:
    key = (retailer, catalog_id)
    if key in dormant:
        return "dormant"
    disc = discovery.get(key)
    if disc and disc.get("state") == "review" and disc.get("review_category") == "challenge":
        return "challenge"
    if key in approved:
        return "approved"
    if key in rejected:
        return "do_not_map"
    if disc:
        state = disc.get("state") or "unmapped"
        if state == "review":
            return "pending"
        if state in {"pending", "unmapped", "rejected", "do_not_map", "inconclusive"}:
            return state if state != "do_not_map" else "do_not_map"
        return state
    return "unmapped"


def coverage_matrix(snapshot: WorkspaceSnapshot) -> dict[str, Any]:
    """Retailer × pack cells from approved JSON mappings (+ discovery when DB)."""
    approved = _approved_mapping_index(snapshot.mappings)
    rejected = _rejected_cells(snapshot.rejections)
    dormant = _dormant_cells(snapshot)
    discovery = _discovery_cell_states(snapshot)
    retailers = list(RETAILER_SLUGS)
    packs: list[dict[str, Any]] = []
    for pack in snapshot.catalog:
        cells: dict[str, dict[str, Any]] = {}
        for retailer in retailers:
            state = _admin_mapping_state(
                retailer=retailer,
                catalog_id=pack.catalog_id,
                approved=approved,
                rejected=rejected,
                dormant=dormant,
                discovery=discovery,
            )
            cells[retailer] = {
                "retailer": retailer,
                "catalog_id": pack.catalog_id,
                "mapping_state": state,
                "approved": state == "approved",
            }
        packs.append(
            {
                "catalog_id": pack.catalog_id,
                "name": pack.name,
                "brand": pack.brand,
                "variant": pack.variant,
                "pack_count": pack.pack_count,
                "unit_size_ml": pack.unit_size_ml,
                "package_type": pack.package_type,
                "approved_count": sum(1 for c in cells.values() if c["approved"]),
                "cells": cells,
            }
        )
    return {
        "retailers": [
            {"slug": r, "display_name": _RETAILER_NAMES[r]} for r in retailers
        ],
        "packs": packs,
        "approved_mappings": len(approved),
    }


def _money_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(value.quantize(_MONEY, rounding=ROUND_HALF_UP))


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _read_current_feed(path: Path) -> list[dict[str, Any]]:
    """Current Feed rows via read-only SQL (same semantics as collector.current_feed)."""
    with closing(_open_readonly(path)) as connection:
        if not _table_exists(connection, "collection_results"):
            return []
        if not _table_exists(connection, "price_observations"):
            return []
        has_mappings = _table_exists(connection, "catalog_mappings")
        has_packs = _table_exists(connection, "catalog_packs")
        dormant_clause = (
            """
            AND NOT EXISTS (
                SELECT 1 FROM catalog_mappings AS cm
                WHERE cm.catalog_id = po.catalog_id
                  AND cm.retailer = po.retailer
                  AND cm.status = 'dormant'
            )
            """
            if has_mappings
            else ""
        )
        pack_join = (
            "LEFT JOIN catalog_packs AS cp ON cp.catalog_id = po.catalog_id"
            if has_packs
            else ""
        )
        # When catalog_packs is absent, still project a null catalog_name.
        columns = _OBSERVATION_COLUMNS if has_packs else _OBSERVATION_COLUMNS.replace(
            "cp.name AS catalog_name",
            "NULL AS catalog_name",
        )
        rows = connection.execute(
            f"""
            WITH latest_results AS (
                SELECT cr.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY cr.retailer, cr.catalog_id
                           ORDER BY cr.recorded_at DESC, cr.rowid DESC
                       ) AS position
                FROM collection_results AS cr
            ),
            latest_observations AS (
                SELECT po.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY po.run_id, po.retailer, po.catalog_id
                           ORDER BY po.observed_at DESC, po.observation_id DESC
                       ) AS obs_position
                FROM price_observations AS po
            )
            SELECT {columns}
            FROM latest_results AS lr
            JOIN latest_observations AS po
              ON po.run_id = lr.run_id
             AND po.catalog_id = lr.catalog_id
             AND po.retailer = lr.retailer
             AND po.obs_position = 1
            {pack_join}
            WHERE lr.position = 1 AND lr.status = 'observed'
              {dormant_clause}
            ORDER BY po.retailer, po.catalog_id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def _read_last_seen(
    path: Path, *, retailer: str, catalog_id: str
) -> dict[str, Any] | None:
    """Latest successful observation with current / not_seen_since availability."""
    with closing(_open_readonly(path)) as connection:
        if not _table_exists(connection, "price_observations"):
            return None
        has_packs = _table_exists(connection, "catalog_packs")
        has_mappings = _table_exists(connection, "catalog_mappings")
        columns = _OBSERVATION_COLUMNS if has_packs else _OBSERVATION_COLUMNS.replace(
            "cp.name AS catalog_name",
            "NULL AS catalog_name",
        )
        pack_join = (
            "LEFT JOIN catalog_packs AS cp ON cp.catalog_id = po.catalog_id"
            if has_packs
            else ""
        )
        dormant_filter = ""
        if has_mappings:
            dormant_filter = (
                "AND (cm.status IS NULL OR cm.status <> 'dormant')"
            )
            mapping_join = (
                "LEFT JOIN catalog_mappings AS cm "
                "ON cm.catalog_id = po.catalog_id AND cm.retailer = po.retailer"
            )
        else:
            mapping_join = ""
        row = connection.execute(
            f"""
            SELECT {columns}
            FROM price_observations AS po
            {pack_join}
            {mapping_join}
            WHERE po.retailer = ? AND po.catalog_id = ?
              {dormant_filter}
            ORDER BY po.observed_at DESC, po.observation_id DESC
            LIMIT 1
            """,
            (retailer, catalog_id),
        ).fetchone()
        if row is None:
            return None
        observation = dict(row)

    # Determine whether this pair is still in the Current Feed.
    in_feed = any(
        r["retailer"] == retailer and r["catalog_id"] == catalog_id
        for r in _read_current_feed(path)
    )
    return observation | {
        "availability": "current" if in_feed else "not_seen_since",
        "not_seen_since": None if in_feed else observation["observed_at"],
    }


def _current_feed_index(
    snapshot: WorkspaceSnapshot,
) -> dict[tuple[str, str], dict[str, Any]]:
    if not snapshot.database.openable:
        return {}
    try:
        rows = _read_current_feed(snapshot.database.path)
    except (sqlite3.Error, OSError):
        return {}
    return {(row["retailer"], row["catalog_id"]): row for row in rows}


def _collection_cell_state(
    *,
    mapping_state: str,
    latest_result: dict[str, Any] | None,
    in_current_feed: bool,
    has_last_seen: bool,
) -> str:
    """Admin collection vocabulary for one retailer–pack cell."""
    if mapping_state in {"unmapped", "do_not_map", "rejected"}:
        return "unmapped"
    if mapping_state == "dormant":
        return "unmapped"
    if latest_result is None:
        return "not_collected"
    status = latest_result.get("status")
    if status == "observed" and in_current_feed:
        return "observed"
    if status == "source_error":
        return "source_error"
    if status == "not_found":
        return "not_found"
    if status == "unmapped":
        return "unmapped"
    if has_last_seen and not in_current_feed:
        return "not_found"
    return status or "not_collected"


def pack_detail(snapshot: WorkspaceSnapshot, catalog_id: str) -> dict[str, Any] | None:
    """Mappings, latest Collection Result, and Last Seen for one pack."""
    pack = next((p for p in snapshot.catalog if p.catalog_id == catalog_id), None)
    if pack is None:
        return None
    approved = _approved_mapping_index(snapshot.mappings)
    rejected = _rejected_cells(snapshot.rejections)
    dormant = _dormant_cells(snapshot)
    discovery = _discovery_cell_states(snapshot)
    latest_results = _latest_collection_results(snapshot)
    feed = _current_feed_index(snapshot)

    retailers: list[dict[str, Any]] = []
    for retailer in SUPPORTED_RETAILERS:
        slug = retailer["slug"]
        key = (slug, catalog_id)
        mapping_state = _admin_mapping_state(
            retailer=slug,
            catalog_id=catalog_id,
            approved=approved,
            rejected=rejected,
            dormant=dormant,
            discovery=discovery,
        )
        mapping_row = approved.get(key)
        latest = latest_results.get(key)
        current = feed.get(key)
        seen: dict[str, Any] | None = None
        if snapshot.database.openable and mapping_state == "approved":
            try:
                seen = _read_last_seen(
                    snapshot.database.path, retailer=slug, catalog_id=catalog_id
                )
            except (sqlite3.Error, OSError):
                seen = None
        collection_state = _collection_cell_state(
            mapping_state=mapping_state,
            latest_result=latest,
            in_current_feed=current is not None,
            has_last_seen=seen is not None,
        )
        observation_state = (
            "current"
            if current is not None
            else "not_seen_since"
            if seen is not None
            else "never_observed"
        )
        retailers.append(
            {
                "retailer": slug,
                "display_name": retailer["display_name"],
                "mapping_state": mapping_state,
                "mapping": mapping_row,
                "collection_state": collection_state,
                "latest_result": latest,
                "current_observation": current,
                "last_seen": seen,
                "observation_state": observation_state,
            }
        )

    return {
        "catalog_id": pack.catalog_id,
        "name": pack.name,
        "brand": pack.brand,
        "variant": pack.variant,
        "pack_count": pack.pack_count,
        "unit_size_ml": pack.unit_size_ml,
        "package_type": pack.package_type,
        "search_term": pack.search_term,
        "retailers": retailers,
    }


def _consumer_cell(
    *,
    pack: BenchmarkPack,
    retailer_slug: str,
    display_name: str,
    mapping_state: str,
    current: dict[str, Any] | None,
    seen: dict[str, Any] | None,
    latest_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build one consumer-facing retailer slot, or None to omit (dormant)."""
    if mapping_state == "dormant":
        return None

    base = {
        "retailer": retailer_slug,
        "display_name": display_name,
        "displayed_price": None,
        "clubcard_price": None,
        "drs_deposit": None,
        "component_unit_price": None,
        "source_scope": None,
        "observed_at": None,
        "currency": "EUR",
    }

    if mapping_state != "approved":
        return {
            **base,
            "state": "not_available",
            "label": "Not available",
        }

    if current is not None:
        price = _as_decimal(current.get("displayed_price"))
        component = None
        if pack.pack_count > 1 and price is not None:
            component = _money_text(price / Decimal(pack.pack_count))
        elif current.get("component_unit_price"):
            component = str(current["component_unit_price"])
        return {
            **base,
            "state": "observed",
            "label": "Observed",
            "displayed_price": str(current.get("displayed_price"))
            if current.get("displayed_price") is not None
            else None,
            "clubcard_price": current.get("clubcard_price"),
            "drs_deposit": current.get("drs_deposit"),
            "component_unit_price": component if pack.pack_count > 1 else None,
            "source_scope": current.get("source_scope"),
            "observed_at": current.get("observed_at"),
            "currency": current.get("currency") or "EUR",
        }

    if latest_result and latest_result.get("status") == "source_error":
        return {
            **base,
            "state": "temporarily_unavailable",
            "label": "Temporarily unavailable",
        }

    if seen is not None and seen.get("availability") == "not_seen_since":
        return {
            **base,
            "state": "last_seen",
            "label": "Last seen",
            "displayed_price": None,  # never present old price as current
            "last_seen_at": seen.get("not_seen_since") or seen.get("observed_at"),
            "observed_at": seen.get("observed_at"),
            "source_scope": seen.get("source_scope"),
        }

    # Approved mapping, no successful observation ever (or only not_found).
    return {
        **base,
        "state": "awaiting_price",
        "label": "Awaiting price",
    }


def feed_preview(
    snapshot: WorkspaceSnapshot,
    *,
    catalog_id: str | None = None,
) -> dict[str, Any]:
    """Exact-Pack Comparison rows for the Consumer Feed Preview (ticket 02)."""
    approved = _approved_mapping_index(snapshot.mappings)
    rejected = _rejected_cells(snapshot.rejections)
    dormant = _dormant_cells(snapshot)
    discovery = _discovery_cell_states(snapshot)
    latest_results = _latest_collection_results(snapshot)
    feed = _current_feed_index(snapshot)

    packs_out: list[dict[str, Any]] = []
    for pack in snapshot.catalog:
        if catalog_id is not None and pack.catalog_id != catalog_id:
            continue
        # Default list scope: packs with ≥1 approved mapping.
        approved_for_pack = [
            slug
            for slug in RETAILER_SLUGS
            if (slug, pack.catalog_id) in approved
            and (slug, pack.catalog_id) not in dormant
        ]
        if not approved_for_pack:
            continue

        cells: list[dict[str, Any]] = []
        observed_prices: list[Decimal] = []
        for retailer in SUPPORTED_RETAILERS:
            slug = retailer["slug"]
            mapping_state = _admin_mapping_state(
                retailer=slug,
                catalog_id=pack.catalog_id,
                approved=approved,
                rejected=rejected,
                dormant=dormant,
                discovery=discovery,
            )
            key = (slug, pack.catalog_id)
            seen = None
            if snapshot.database.openable and mapping_state == "approved":
                try:
                    seen = _read_last_seen(
                        snapshot.database.path,
                        retailer=slug,
                        catalog_id=pack.catalog_id,
                    )
                except (sqlite3.Error, OSError):
                    seen = None
            cell = _consumer_cell(
                pack=pack,
                retailer_slug=slug,
                display_name=retailer["display_name"],
                mapping_state=mapping_state,
                current=feed.get(key),
                seen=seen,
                latest_result=latest_results.get(key),
            )
            if cell is None:
                continue
            if cell["state"] == "observed" and cell.get("displayed_price") is not None:
                price = _as_decimal(cell["displayed_price"])
                if price is not None:
                    observed_prices.append(price)
            cells.append(cell)

        best = min(observed_prices) if observed_prices else None
        for cell in cells:
            if cell["state"] == "observed" and cell.get("displayed_price") is not None:
                price = _as_decimal(cell["displayed_price"])
                cell["is_best"] = bool(best is not None and price == best)
            else:
                cell["is_best"] = False

        packs_out.append(
            {
                "catalog_id": pack.catalog_id,
                "name": pack.name,
                "brand": pack.brand,
                "variant": pack.variant,
                "pack_count": pack.pack_count,
                "unit_size_ml": pack.unit_size_ml,
                "package_type": pack.package_type,
                "pack_label": (
                    f"{pack.brand} {pack.variant} · "
                    f"{pack.pack_count}×{pack.unit_size_ml}ml {pack.package_type}"
                ),
                "retailers": cells,
            }
        )

    return {
        "standing_rule": "A missing price is not a stock or retirement claim.",
        "retailers": [
            {"slug": r["slug"], "display_name": r["display_name"]}
            for r in SUPPORTED_RETAILERS
        ],
        "packs": packs_out,
        "pack_count": len(packs_out),
    }


def catalog_table(snapshot: WorkspaceSnapshot) -> list[dict[str, Any]]:
    """Short Benchmark Catalog rows for Overview / catalog list."""
    matrix = coverage_matrix(snapshot)
    feed = _current_feed_index(snapshot)
    latest = _latest_collection_results(snapshot)
    rows: list[dict[str, Any]] = []
    for pack in matrix["packs"]:
        observed = 0
        awaiting = 0
        for slug, cell in pack["cells"].items():
            if not cell["approved"]:
                continue
            key = (slug, pack["catalog_id"])
            if key in feed:
                observed += 1
            else:
                result = latest.get(key)
                if result and result.get("status") == "source_error":
                    pass
                awaiting += 1
        if observed:
            feed_state = "partial" if awaiting else "observed"
            feed_label = f"{observed} observed"
        elif pack["approved_count"]:
            feed_state = "awaiting"
            feed_label = "Awaiting price"
        else:
            feed_state = "unmapped"
            feed_label = "No approved mappings"
        rows.append(
            {
                "catalog_id": pack["catalog_id"],
                "name": pack["name"],
                "brand": pack["brand"],
                "pack_count": pack["pack_count"],
                "unit_size_ml": pack["unit_size_ml"],
                "package_type": pack["package_type"],
                "approved_count": pack["approved_count"],
                "retailer_count": len(RETAILER_SLUGS),
                "feed_state": feed_state,
                "feed_label": feed_label,
            }
        )
    return rows


def raw_results(
    snapshot: WorkspaceSnapshot,
    *,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Raw collection results: every retailer-pack decision, newest first.

    This is the "why is my product missing?" view: unmapped, not_found and
    source_error cells are all shown with the recorded reason, unlike the
    curated feed which only surfaces observed prices.
    """
    if not snapshot.database.openable:
        return []
    with closing(_open_readonly(snapshot.database.path)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT cr.recorded_at, cr.retailer, cr.catalog_id,
                       cp.name AS pack_name, cr.status, cr.error,
                       cr.source_product_reference, cr.source_item_id
                FROM collection_results AS cr
                LEFT JOIN catalog_packs AS cp ON cp.catalog_id = cr.catalog_id
                ORDER BY cr.recorded_at DESC, cr.rowid DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def raw_candidates(
    snapshot: WorkspaceSnapshot,
    *,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Raw Catalog Candidates: scraped listings not tied to a catalog pack.

    Products "found in scraping" but absent from every curated view live
    here until an operator approves a mapping.
    """
    if not snapshot.database.openable:
        return []
    with closing(_open_readonly(snapshot.database.path)) as connection:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT first_seen_at, retailer, candidate_id,
                       source_product_name, displayed_price, status,
                       source_product_reference, source_item_id
                FROM catalog_candidates
                ORDER BY first_seen_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def iter_readonly_connections(
    snapshot: WorkspaceSnapshot,
) -> Iterator[sqlite3.Connection]:
    """Test helper: yield a read-only connection when the DB is openable."""
    if snapshot.database.openable:
        with closing(_open_readonly(snapshot.database.path)) as connection:
            yield connection
