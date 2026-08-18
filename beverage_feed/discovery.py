"""Durable state for Catalog Mapping discovery.

Discovery records evidence and mapping decisions only.  Price observations are
written by :mod:`beverage_feed.collector`, never by this module.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .collector import ensure_schema, safe_record, timestamp


CELL_STATES = {
    "pending", "approved", "review", "unmapped", "inconclusive",
    "identity_unstable", "rejected", "do_not_map",
}
REVIEW_CATEGORIES = {"missing", "conflicting", "conflicting-candidates", "challenge"}
RUN_STATUSES = {"running", "complete", "paused", "budget_exhausted"}
COMPLETENESS = {True, False, "unknown"}
PRICE_PARSE_STATUSES = {"valid", "missing", "malformed", "unsupported_promotion"}

_DISCOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    request_counts TEXT NOT NULL DEFAULT '{}',
    batch_sizes TEXT NOT NULL DEFAULT '{}',
    cells_advanced INTEGER NOT NULL DEFAULT 0,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS discovery_attempts (
    attempt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    request_counts TEXT NOT NULL DEFAULT '{}',
    batch_sizes TEXT NOT NULL DEFAULT '{}',
    cells_advanced INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id)
);
CREATE TABLE IF NOT EXISTS discovery_cells (
    retailer TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    state TEXT NOT NULL,
    review_category TEXT,
    candidate_id TEXT,
    decided_at TEXT,
    decided_by TEXT,
    reason TEXT,
    PRIMARY KEY (retailer, catalog_id)
);
CREATE TABLE IF NOT EXISTS discovery_candidate_cells (
    candidate_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    search_terms TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (candidate_id, retailer, catalog_id)
);
CREATE TABLE IF NOT EXISTS discovery_candidate_search_terms (
    candidate_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    search_term TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (candidate_id, retailer, search_term)
);
CREATE TABLE IF NOT EXISTS discovery_search_history (
    search_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    attempt_id TEXT,
    retailer TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    search_term TEXT NOT NULL,
    searched_at TEXT NOT NULL,
    complete TEXT NOT NULL,
    request_kind TEXT NOT NULL DEFAULT 'search',
    request_metadata TEXT,
    FOREIGN KEY (run_id) REFERENCES discovery_runs(run_id)
);
CREATE TABLE IF NOT EXISTS discovery_candidate_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL,
    retailer TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    raw_attributes TEXT NOT NULL DEFAULT '{}',
    normalized_attributes TEXT NOT NULL DEFAULT '{}',
    inference_basis TEXT NOT NULL DEFAULT '{}',
    attribute_diffs TEXT NOT NULL DEFAULT '{}',
    raw_price_value TEXT,
    price_parse_status TEXT,
    price_parse_reason TEXT
);
CREATE TABLE IF NOT EXISTS discovery_state_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    catalog_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    category TEXT,
    candidate_id TEXT,
    reason TEXT,
    changed_at TEXT NOT NULL,
    changed_by TEXT
);
CREATE TABLE IF NOT EXISTS discovery_identity_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer TEXT NOT NULL,
    weak_candidate_id TEXT NOT NULL,
    strong_candidate_id TEXT NOT NULL,
    relationship TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS discovery_rejections (
    section TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    retailer TEXT NOT NULL,
    catalog_id TEXT,
    rejected_at TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT,
    state TEXT NOT NULL,
    superseded_at TEXT,
    PRIMARY KEY (section, canonical_key, rejected_at)
);
CREATE TABLE IF NOT EXISTS discovery_diagnostics (
    diagnostic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    attempt_id TEXT,
    retailer TEXT,
    catalog_id TEXT,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT,
    details TEXT,
    created_at TEXT NOT NULL
);
"""

# Existing collection tables predate discovery.  These columns keep the old
# collector compatible while giving discovery decisions durable provenance.
_CANDIDATE_COLUMNS = {
    "identity_key": "TEXT",
    "identity_basis": "TEXT",
    "identity_tier": "TEXT",
}
_MAPPING_COLUMNS = {
    "decision_kind": "TEXT",
    "decided_by": "TEXT",
    "decided_at": "TEXT",
    "discovery_run_id": "TEXT",
    "matched_source_identity": "TEXT",
    "identity_tier": "TEXT",
    "candidate_id": "TEXT",
    "decision_reason": "TEXT",
}


def _json(value: Any, default: Any = None) -> str | None:
    if value is None:
        value = default
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_discovery_schema(database: str | Path) -> None:
    """Create or migrate discovery tables without touching observations."""
    database_path = Path(database)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        ensure_schema(connection)
        _ensure_columns(connection, "catalog_candidates", _CANDIDATE_COLUMNS)
        _ensure_columns(connection, "catalog_mappings", _MAPPING_COLUMNS)
        connection.executescript(_DISCOVERY_SCHEMA)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS catalog_candidates_retailer_identity "
            "ON catalog_candidates(retailer, identity_key) WHERE identity_key IS NOT NULL"
        )
        connection.commit()


def _read_json(path: str | Path, *, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc


_MAPPING_KEYS = {
    "catalog_id", "expected_product_name", "source_product_reference", "source_item_id",
    "source_product_id", "source_tpnb", "status", "decision_kind", "decided_by",
    "decided_at", "discovery_run_id", "matched_source_identity", "identity_tier",
    "candidate_id", "decision_reason", "auto_approved", "approved_at", "superseded_by",
}
_REQUIRED_MAPPING_KEYS = {"catalog_id", "expected_product_name", "status"}
_MAPPING_STATUSES = {"approved", "review", "unmapped", "rejected", "dormant"}
_MAPPING_SOURCE_KEYS = {
    "dunnes": {"source_product_reference", "source_item_id"},
    "supervalu": {"source_product_id"},
    "tesco": {"source_tpnb"},
}
_REJECTION_LISTING_KEYS = {
    "canonical_key", "retailer", "catalog_id", "cell", "rejected_at", "decided_by", "reason",
    "state", "superseded_at",
}
_REJECTION_CELL_KEYS = {
    "retailer", "catalog_id", "cell", "rejected_at", "decided_by", "reason", "state", "superseded_at",
}


def _validate_mapping_object(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValueError("mapping file must contain a retailer object")
    result: dict[str, list[dict[str, Any]]] = {}
    for retailer, rows in value.items():
        if retailer not in {"dunnes", "supervalu", "tesco"}:
            raise ValueError(f"unsupported retailer in mapping file: {retailer}")
        if not isinstance(rows, list):
            raise ValueError(f"mappings for {retailer} must be a list")
        clean_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("mapping entries must be objects")
            unknown = set(row) - _MAPPING_KEYS
            invalid_source = (set(row) & {"source_product_reference", "source_item_id", "source_product_id", "source_tpnb"}) - _MAPPING_SOURCE_KEYS[retailer]
            missing = _REQUIRED_MAPPING_KEYS - set(row)
            if unknown:
                raise ValueError(f"unknown mapping fields: {sorted(unknown)}")
            if invalid_source:
                raise ValueError(f"source fields do not belong to {retailer}: {sorted(invalid_source)}")
            if missing:
                raise ValueError(f"mapping entry missing fields: {sorted(missing)}")
            if not all(isinstance(row[key], str) and row[key].strip() for key in _REQUIRED_MAPPING_KEYS):
                raise ValueError("mapping identity and name fields must be non-empty strings")
            if row["status"] not in _MAPPING_STATUSES:
                raise ValueError(f"unsupported mapping status: {row['status']}")
            for key in set(row) - _REQUIRED_MAPPING_KEYS - {"auto_approved"}:
                if row[key] is not None and not isinstance(row[key], str):
                    raise ValueError(f"mapping field {key} must be a string or null")
            if "auto_approved" in row and not isinstance(row["auto_approved"], bool):
                raise ValueError("auto_approved must be boolean")
            clean_rows.append(dict(row))
        approved_cells = [row["catalog_id"] for row in clean_rows if row["status"] == "approved"]
        duplicates = sorted({cell for cell in approved_cells if approved_cells.count(cell) > 1})
        if duplicates:
            raise ValueError(f"multiple approved mappings for retailer-pack cells: {duplicates}")
        result[retailer] = clean_rows
    return result


def load_mappings(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    return _validate_mapping_object(_read_json(path, default={}))


def _validate_rejections(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != {"listings", "cells"}:
        raise ValueError("rejection file must contain exactly listings and cells sections")
    result: dict[str, list[dict[str, Any]]] = {"listings": [], "cells": []}
    for section, rows in value.items():
        if not isinstance(rows, list):
            raise ValueError(f"rejection section {section} must be a list")
        allowed = _REJECTION_LISTING_KEYS if section == "listings" else _REJECTION_CELL_KEYS
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("rejection entries must be objects")
            unknown = set(row) - allowed
            required = {"canonical_key", "retailer", "rejected_at", "decided_by", "state"} if section == "listings" else {"retailer", "rejected_at", "decided_by", "state"}
            has_cell = "cell" in row or "catalog_id" in row
            if unknown or not required.issubset(row) or not has_cell:
                raise ValueError(f"invalid {section[:-1]} rejection fields")
            if section == "listings" and row["state"] not in {"rejected", "superseded"}:
                raise ValueError("listing rejection state must be rejected or superseded")
            if section == "cells" and row["state"] not in {"do_not_map", "superseded"}:
                raise ValueError("cell rejection state must be do_not_map or superseded")
            if any(not isinstance(row[key], str) or not row[key].strip() for key in required):
                raise ValueError("rejection identity fields must be non-empty strings")
            cell = row.get("cell", row.get("catalog_id"))
            if not isinstance(cell, str) or not cell.strip():
                raise ValueError("rejection cell must be a non-empty string")
            result[section].append(dict(row))
    return result


def load_rejections(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    return _validate_rejections(_read_json(path, default={"listings": [], "cells": []}))


def _atomic_json_write(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_mappings(path: str | Path, mappings: Mapping[str, Any]) -> None:
    _atomic_json_write(path, _validate_mapping_object(dict(mappings)))


def write_rejections(path: str | Path, rejections: Mapping[str, Any]) -> None:
    _atomic_json_write(path, _validate_rejections(dict(rejections)))


def _mapping_source_identity(retailer: str, row: Mapping[str, Any]) -> str | None:
    if retailer == "dunnes":
        values = [row.get("source_product_reference"), row.get("source_item_id")]
    elif retailer == "supervalu":
        values = [row.get("source_product_id")]
    else:
        values = [row.get("source_tpnb")]
    parts = [str(value) for value in values if value]
    return ":".join(parts) or None


def candidate_id_for(retailer: str, identity: str) -> str:
    """Canonical Catalog Candidate id for a retailer identity pair."""
    return f"{retailer}:{identity}"


def approved_mapping(
    mappings: Mapping[str, list[dict[str, Any]]], retailer: str, catalog_id: str,
) -> dict[str, Any] | None:
    """The single approved mapping row for a cell, if any."""
    return next(
        (
            row
            for row in mappings.get(retailer, [])
            if row["catalog_id"] == catalog_id and row["status"] == "approved"
        ),
        None,
    )


def source_fields(retailer: str, identity: str, identity_tier: str | None = None) -> dict[str, str]:
    """Retailer mapping source fields for a candidate identity."""
    if retailer == "dunnes":
        reference, _, item = identity.partition(":")
        if identity_tier == "composite":
            return {"source_product_reference": reference, "source_item_id": item}
        if identity_tier == "item":
            return {"source_item_id": identity}
        if identity_tier == "product":
            return {"source_product_reference": identity}
        return {"source_product_reference": reference, "source_item_id": item or reference}
    if retailer == "supervalu":
        return {"source_product_id": identity}
    return {"source_tpnb": identity}



def _assert_no_rejected_mapping(mappings: Mapping[str, list[dict[str, Any]]], rejections: Mapping[str, list[dict[str, Any]]]) -> None:
    rejected = {
        (row["retailer"], row.get("cell", row.get("catalog_id")), row.get("canonical_key"))
        for row in rejections["listings"]
        if row["state"] == "rejected"
    }
    for retailer, rows in mappings.items():
        for row in rows:
            identity = _mapping_source_identity(retailer, row)
            keys = {f"{retailer}:{identity}" if identity else None, row.get("candidate_id")}
            if row.get("status") == "approved" and any(
                (retailer, row["catalog_id"], key) in rejected for key in keys
            ):
                raise ValueError("rejected candidate cannot also be an approved mapping")


class DiscoveryStore:
    """Small persistence seam for discovery state and evidence."""

    def __init__(self, database: str | Path):
        self.database = Path(database)
        ensure_discovery_schema(self.database)

    def connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def start_run(self, run_id: str | None = None, *, started_at: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        with closing(self.connection()) as connection:
            connection.execute(
                "INSERT INTO discovery_runs(run_id, started_at, status) VALUES (?, ?, 'running')",
                (run_id, started_at or timestamp()),
            )
            connection.commit()
        return run_id

    def start_attempt(self, run_id: str, attempt_id: str | None = None, *, started_at: str | None = None) -> str:
        attempt_id = attempt_id or uuid.uuid4().hex
        with closing(self.connection()) as connection:
            connection.execute(
                "INSERT INTO discovery_attempts(attempt_id, run_id, started_at) VALUES (?, ?, ?)",
                (attempt_id, run_id, started_at or timestamp()),
            )
            connection.commit()
        return attempt_id

    def finish_run(self, run_id: str, status: str, *, finished_at: str | None = None, request_counts: Mapping[str, int] | None = None, batch_sizes: Mapping[str, int] | None = None, cells_advanced: int = 0, summary: Mapping[str, Any] | None = None) -> None:
        if status not in RUN_STATUSES - {"running"}:
            raise ValueError(f"invalid discovery run status: {status}")
        with closing(self.connection()) as connection:
            connection.execute(
                "UPDATE discovery_runs SET finished_at=?, status=?, request_counts=?, batch_sizes=?, cells_advanced=?, summary=? WHERE run_id=?",
                (finished_at or timestamp(), status, _json(request_counts, {}), _json(batch_sizes, {}), cells_advanced, _json(summary, {}), run_id),
            )
            connection.commit()

    def finish_attempt(self, run_id: str, attempt_id: str, *, status: str = "complete", finished_at: str | None = None, request_counts: Mapping[str, int] | None = None, batch_sizes: Mapping[str, int] | None = None, cells_advanced: int = 0) -> None:
        if status not in RUN_STATUSES - {"running"}:
            raise ValueError(f"invalid discovery attempt status: {status}")
        with closing(self.connection()) as connection:
            connection.execute(
                "UPDATE discovery_attempts SET finished_at=?, status=?, request_counts=?, batch_sizes=?, cells_advanced=? WHERE attempt_id=? AND run_id=?",
                (finished_at or timestamp(), status, _json(request_counts, {}), _json(batch_sizes, {}), cells_advanced, attempt_id, run_id),
            )
            connection.commit()

    def upsert_candidate(self, candidate_id: str, *, retailer: str, identity_key: str, identity_basis: str, identity_tier: str, source_product_reference: str = "", source_item_id: str = "", source_product_name: str = "", raw_record: Any = None, displayed_price: str | None = None, first_seen_at: str | None = None) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (candidate_id, retailer, identity_key, identity_basis, identity_tier)):
            raise ValueError("Catalog Candidate identity fields must be non-empty strings")
        now = first_seen_at or timestamp()
        with closing(self.connection()) as connection:
            existing = connection.execute(
                "SELECT candidate_id FROM catalog_candidates WHERE retailer=? AND identity_key=?",
                (retailer, identity_key),
            ).fetchone()
            if existing is not None:
                candidate_id = existing[0]
            connection.execute(
                """
                INSERT INTO catalog_candidates
                    (candidate_id, retailer, source_product_reference, source_item_id,
                     source_product_name, displayed_price, raw_record, status, first_seen_at,
                     identity_key, identity_basis, identity_tier)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    displayed_price=excluded.displayed_price, raw_record=excluded.raw_record,
                    identity_key=excluded.identity_key, identity_basis=excluded.identity_basis,
                    identity_tier=excluded.identity_tier
                """,
                (candidate_id, retailer, source_product_reference, source_item_id, source_product_name, displayed_price, safe_record(raw_record) or "{}", now, identity_key, identity_basis, identity_tier),
            )
            connection.commit()

    def associate_candidate(self, candidate_id: str, catalog_id: str, search_term: str, *, retailer: str | None = None, seen_at: str | None = None) -> None:
        seen_at = seen_at or timestamp()
        with closing(self.connection()) as connection:
            if retailer is None:
                candidate_row = connection.execute("SELECT retailer FROM catalog_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
                if candidate_row is None:
                    raise ValueError(f"unknown Catalog Candidate: {candidate_id}")
                retailer = candidate_row[0]
            existing = connection.execute(
                "SELECT search_terms FROM discovery_candidate_cells WHERE candidate_id=? AND retailer=? AND catalog_id=?",
                (candidate_id, retailer, catalog_id),
            ).fetchone()
            search_terms = _loads(existing[0], []) if existing else []
            if search_term not in search_terms:
                search_terms.append(search_term)
            connection.execute(
                """
                INSERT INTO discovery_candidate_cells(candidate_id, retailer, catalog_id, first_seen_at, last_seen_at, search_terms)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id, retailer, catalog_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at, search_terms=excluded.search_terms
                """,
                (candidate_id, retailer, catalog_id, seen_at, seen_at, _json(search_terms)),
            )
            connection.execute(
                """
                INSERT INTO discovery_candidate_search_terms(candidate_id, retailer, search_term, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id, retailer, search_term) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                (candidate_id, retailer, search_term, seen_at, seen_at),
            )
            connection.commit()

    def record_search(self, run_id: str, attempt_id: str | None, catalog_id: str, retailer: str, search_term: str, *, complete: bool | str, request_kind: str = "search", request_metadata: Any = None, searched_at: str | None = None) -> int:
        if complete not in COMPLETENESS:
            raise ValueError("search completeness must be true, false, or unknown")
        complete_text = "unknown" if complete == "unknown" else str(complete).lower()
        with closing(self.connection()) as connection:
            cursor = connection.execute(
                "INSERT INTO discovery_search_history(run_id, attempt_id, retailer, catalog_id, search_term, searched_at, complete, request_kind, request_metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, attempt_id, retailer, catalog_id, search_term, searched_at or timestamp(), complete_text, request_kind, safe_record(request_metadata)),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)

    def record_evidence(self, candidate_id: str, catalog_id: str, *, retailer: str, raw_attributes: Any = None, normalized_attributes: Any = None, inference_basis: Any = None, attribute_diffs: Any = None, raw_price_value: Any = None, price_parse_status: str | None = None, price_parse_reason: str | None = None, recorded_at: str | None = None) -> int:
        if price_parse_status is not None and price_parse_status not in PRICE_PARSE_STATUSES:
            raise ValueError(f"invalid price parse status: {price_parse_status}")
        with closing(self.connection()) as connection:
            cursor = connection.execute(
                "INSERT INTO discovery_candidate_evidence(candidate_id, retailer, catalog_id, recorded_at, raw_attributes, normalized_attributes, inference_basis, attribute_diffs, raw_price_value, price_parse_status, price_parse_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (candidate_id, retailer, catalog_id, recorded_at or timestamp(), _json(raw_attributes, {}), _json(normalized_attributes, {}), _json(inference_basis, {}), _json(attribute_diffs, {}), None if raw_price_value is None else str(raw_price_value), price_parse_status, price_parse_reason),
            )
            connection.commit()
            return int(cursor.lastrowid or 0)

    def set_cell_state(self, retailer: str, catalog_id: str, state: str, *, review_category: str | None = None, candidate_id: str | None = None, decided_by: str | None = None, reason: str | None = None, changed_at: str | None = None) -> None:
        if state not in CELL_STATES:
            raise ValueError(f"invalid discovery cell state: {state}")
        if review_category is not None and review_category not in REVIEW_CATEGORIES:
            raise ValueError(f"invalid review category: {review_category}")
        changed_at = changed_at or timestamp()
        with closing(self.connection()) as connection:
            previous = connection.execute("SELECT state FROM discovery_cells WHERE retailer=? AND catalog_id=?", (retailer, catalog_id)).fetchone()
            connection.execute(
                "INSERT INTO discovery_cells(retailer, catalog_id, state, review_category, candidate_id, decided_at, decided_by, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(retailer, catalog_id) DO UPDATE SET state=excluded.state, review_category=excluded.review_category, candidate_id=excluded.candidate_id, decided_at=excluded.decided_at, decided_by=excluded.decided_by, reason=excluded.reason",
                (retailer, catalog_id, state, review_category, candidate_id, changed_at, decided_by, reason),
            )
            connection.execute(
                "INSERT INTO discovery_state_transitions(retailer, catalog_id, from_state, to_state, category, candidate_id, reason, changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (retailer, catalog_id, previous[0] if previous else None, state, review_category, candidate_id, reason, changed_at, decided_by),
            )
            connection.commit()

    def link_identity(self, retailer: str, weak_candidate_id: str, strong_candidate_id: str, *, relationship: str = "upgrade", reason: str | None = None, created_at: str | None = None) -> None:
        with closing(self.connection()) as connection:
            connection.execute("INSERT INTO discovery_identity_links(retailer, weak_candidate_id, strong_candidate_id, relationship, created_at, reason) VALUES (?, ?, ?, ?, ?, ?)", (retailer, weak_candidate_id, strong_candidate_id, relationship, created_at or timestamp(), reason))
            connection.commit()

    def reject_candidate(self, *, retailer: str, candidate_id: str, catalog_id: str, decided_by: str, reason: str | None = None, rejected_at: str | None = None, state: str = "rejected") -> str:
        if state not in {"rejected", "superseded"}:
            raise ValueError("candidate rejection state must be rejected or superseded")
        rejected_at = rejected_at or timestamp()
        canonical_key = candidate_id
        with closing(self.connection()) as connection:
            connection.execute(
                "INSERT INTO discovery_rejections(section, canonical_key, retailer, catalog_id, rejected_at, decided_by, reason, state) VALUES ('listings', ?, ?, ?, ?, ?, ?, ?)",
                (canonical_key, retailer, catalog_id, rejected_at, decided_by, reason, state),
            )
            if state == "rejected":
                connection.execute(
                    "UPDATE catalog_candidates SET status='rejected' WHERE candidate_id=?",
                    (candidate_id,),
                )
            connection.commit()
        return canonical_key

    def inherit_rejection(self, *, retailer: str, weak_candidate_id: str, strong_candidate_id: str, catalog_id: str, decided_by: str, reason: str | None = None, rejected_at: str | None = None) -> None:
        """Link an identity upgrade and carry an active rejection forward."""
        rejected_at = rejected_at or timestamp()
        with closing(self.connection()) as connection:
            connection.execute(
                "INSERT INTO discovery_identity_links(retailer, weak_candidate_id, strong_candidate_id, relationship, created_at, reason) VALUES (?, ?, ?, 'rejection_inherits', ?, ?)",
                (retailer, weak_candidate_id, strong_candidate_id, rejected_at, reason),
            )
            connection.execute(
                "INSERT INTO discovery_rejections(section, canonical_key, retailer, catalog_id, rejected_at, decided_by, reason, state) VALUES ('listings', ?, ?, ?, ?, ?, ?, 'rejected')",
                (strong_candidate_id, retailer, catalog_id, rejected_at, decided_by, reason),
            )
            connection.execute(
                "UPDATE catalog_candidates SET status='rejected' WHERE candidate_id=?",
                (strong_candidate_id,),
            )
            connection.commit()

    def supersede_rejection(self, section: str, canonical_key: str, *, superseded_at: str | None = None) -> int:
        if section not in {"listings", "cells"}:
            raise ValueError("rejection section must be listings or cells")
        superseded_at = superseded_at or timestamp()
        with closing(self.connection()) as connection:
            changed = connection.execute(
                "UPDATE discovery_rejections SET state='superseded', superseded_at=? WHERE section=? AND canonical_key=? AND state <> 'superseded'",
                (superseded_at, section, canonical_key),
            ).rowcount
            connection.commit()
        return changed

    def diagnostic(self, *, event: str, level: str = "info", message: str | None = None, run_id: str | None = None, attempt_id: str | None = None, retailer: str | None = None, catalog_id: str | None = None, details: Any = None) -> None:
        with closing(self.connection()) as connection:
            connection.execute("INSERT INTO discovery_diagnostics(run_id, attempt_id, retailer, catalog_id, level, event, message, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (run_id, attempt_id, retailer, catalog_id, level, event, message, safe_record(details), timestamp()))
            connection.commit()


def reconcile_json_decisions(database: str | Path, mapping_path: str | Path, rejection_path: str | Path) -> None:
    """Rebuild SQLite decision state from the committed JSON files.

    This is intentionally JSON-first: a process interrupted after the JSON
    rename but before SQLite commit is repaired by the next invocation.
    """
    mappings = load_mappings(mapping_path)
    rejections = load_rejections(rejection_path)
    _assert_no_rejected_mapping(mappings, rejections)
    ensure_discovery_schema(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        # Fixture case: a challenge review must survive JSON-first reconciliation
        # even though the challenged mapping stays approved in JSON.
        challenges = connection.execute(
            "SELECT retailer, catalog_id, candidate_id, decided_at, decided_by, reason "
            "FROM discovery_cells WHERE state='review' AND review_category='challenge'"
        ).fetchall()
        connection.execute("DELETE FROM catalog_mappings")
        connection.execute("DELETE FROM discovery_rejections")

        def upsert_cell(
            retailer: str,
            catalog_id: str,
            state: str,
            review_category: str | None = None,
            candidate_id: str | None = None,
            decided_at: str | None = None,
            decided_by: str | None = None,
            reason: str | None = None,
        ) -> None:
            connection.execute(
                """
                INSERT INTO discovery_cells
                    (retailer, catalog_id, state, review_category, candidate_id,
                     decided_at, decided_by, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(retailer, catalog_id) DO UPDATE SET
                    state=excluded.state,
                    review_category=excluded.review_category,
                    candidate_id=excluded.candidate_id,
                    decided_at=excluded.decided_at,
                    decided_by=excluded.decided_by,
                    reason=excluded.reason
                """ ,
                (retailer, catalog_id, state, review_category, candidate_id,
                 decided_at, decided_by, reason),
            )

        for retailer, rows in mappings.items():
            for row in rows:
                # Fixture case: superseded mappings remain in JSON history but
                # catalog_mappings keeps only the current row per cell.
                if row.get("superseded_by"):
                    continue
                source_ref = row.get("source_product_reference") or row.get("source_product_id") or row.get("source_tpnb")
                source_item = row.get("source_item_id") or row.get("source_product_id") or row.get("source_tpnb")
                connection.execute(
                    """
                    INSERT INTO catalog_mappings
                      (catalog_id, retailer, expected_product_name, source_product_reference, source_item_id, status,
                       decision_kind, decided_by, decided_at, discovery_run_id, matched_source_identity, identity_tier,
                       candidate_id, decision_reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (row["catalog_id"], retailer, row["expected_product_name"], source_ref, source_item, row["status"], row.get("decision_kind"), row.get("decided_by"), row.get("decided_at") or row.get("approved_at"), row.get("discovery_run_id"), row.get("matched_source_identity") or _mapping_source_identity(retailer, row), row.get("identity_tier"), row.get("candidate_id"), row.get("decision_reason")),
                )
                if row["status"] == "approved":
                    upsert_cell(
                        retailer, row["catalog_id"], "approved",
                        candidate_id=row.get("candidate_id"),
                        decided_at=row.get("decided_at") or row.get("approved_at"),
                        decided_by=row.get("decided_by"),
                        reason=row.get("decision_reason"),
                    )
        for section in ("listings", "cells"):
            for row in rejections[section]:
                cell = row.get("cell", row.get("catalog_id"))
                key = row.get("canonical_key") or f"{row['retailer']}:{cell}"
                connection.execute(
                    "INSERT INTO discovery_rejections(section, canonical_key, retailer, catalog_id, rejected_at, decided_by, reason, state, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (section, key, row["retailer"], cell, row["rejected_at"], row["decided_by"], row.get("reason"), row["state"], row.get("superseded_at")),
                )
                if section == "listings" and row["state"] == "rejected":
                    connection.execute(
                        "UPDATE catalog_candidates SET status='rejected' WHERE candidate_id=?",
                        (key,),
                    )
                if row["state"] == "do_not_map":
                    upsert_cell(
                        row["retailer"], cell, "do_not_map",
                        decided_at=row["rejected_at"],
                        decided_by=row["decided_by"],
                        reason=row.get("reason"),
                    )
                elif row["state"] == "rejected":
                    upsert_cell(
                        row["retailer"], cell, "rejected",
                        decided_at=row["rejected_at"],
                        decided_by=row["decided_by"],
                        reason=row.get("reason"),
                    )
        for challenge_retailer, challenge_catalog, challenge_candidate, challenge_at, challenge_by, challenge_reason in challenges:
            connection.execute(
                "INSERT INTO discovery_cells(retailer, catalog_id, state, review_category, candidate_id, decided_at, decided_by, reason) VALUES (?, ?, 'review', 'challenge', ?, ?, ?, ?) "
                "ON CONFLICT(retailer, catalog_id) DO UPDATE SET state='review', review_category='challenge', candidate_id=excluded.candidate_id, decided_at=excluded.decided_at, decided_by=excluded.decided_by, reason=excluded.reason",
                (challenge_retailer, challenge_catalog, challenge_candidate, challenge_at, challenge_by, challenge_reason),
            )
        connection.commit()


__all__ = [
    "DiscoveryStore", "ensure_discovery_schema", "load_mappings", "write_mappings",
    "load_rejections", "write_rejections", "reconcile_json_decisions",
]
