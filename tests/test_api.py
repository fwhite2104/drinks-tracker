"""Read-only HTTP API coverage against temporary SQLite databases."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from beverage_feed.collector import BenchmarkPack

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


PACK = BenchmarkPack(
    catalog_id="water-5l",
    name="Comeragh Still Water 5L Bottle",
    brand="Comeragh",
    variant="Still Water",
    pack_count=1,
    unit_size_ml=5000,
    package_type="bottle",
    search_term="Still Water",
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A TestClient bound to a fresh database seeded with one observation."""
    from fastapi.testclient import TestClient

    database = tmp_path / "feed.sqlite"
    monkeypatch.setenv("DRINKS_DATABASE", str(database))
    from beverage_feed.api import app
    from beverage_feed.collector import AldiMapping, collect_aldi_one

    record = {
        "productId": "000000000000336021",
        "name": "Still Water",
        "brand": "COMERAGH",
        "price": "\u20ac1.45",
    }
    collect_aldi_one(
        PACK,
        AldiMapping(catalog_id=PACK.catalog_id, expected_product_name="Still Water"),
        lambda _: {"items": [record]},
        database,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_status_database_and_observation_count(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["observations"] == 1
    assert Path(body["database"]).name == "feed.sqlite"


def test_catalog_returns_all_benchmark_packs(client):
    rows = client.get("/catalog").json()

    assert [row["catalog_id"] for row in rows] == [PACK.catalog_id]
    assert rows[0]["pack_count"] == 1


def test_current_prices_filter_by_retailer_and_catalog(client):
    rows = client.get("/prices/current").json()
    filtered = client.get(
        "/prices/current", params={"retailer": "aldi", "catalog_id": PACK.catalog_id}
    ).json()

    assert [(row["retailer"], row["displayed_price"]) for row in rows] == [("aldi", "1.45")]
    assert filtered == rows


def test_history_requires_catalog_id_and_returns_newest_first(client):
    assert client.get("/prices/history").status_code == 422

    rows = client.get("/prices/history", params={"catalog_id": PACK.catalog_id}).json()

    observed_at = [row["observed_at"] for row in rows]
    assert observed_at == sorted(observed_at, reverse=True)


def test_last_seen_returns_pair_or_404_when_never_observed(client):
    found = client.get(
        "/last-seen", params={"retailer": "aldi", "catalog_id": PACK.catalog_id}
    )
    missing = client.get(
        "/last-seen", params={"retailer": "tesco", "catalog_id": PACK.catalog_id}
    )

    assert found.status_code == 200
    assert found.json()["displayed_price"] == "1.45"
    assert missing.status_code == 404


def test_coverage_reports_mapping_approval_and_freshness(client):
    body = client.get("/coverage").json()

    assert body["freshness_days"] == 7
    assert body["per_retailer"] == [{
        "retailer": "aldi", "cells": 1, "approved": 1,
        "review": 0, "fresh_observations": 1,
    }]
    cell = body["cells"][0]
    assert (cell["retailer"], cell["mapping_status"], cell["fresh"]) == (
        "aldi", "approved", True,
    )


def test_startup_fails_clearly_when_database_cannot_be_opened(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    blocked = tmp_path / "missing-dir" / "feed.sqlite"
    blocked.parent.mkdir(parents=True)
    # A directory where the database file should be cannot be opened as SQLite.
    blocked.parent.chmod(0o500)
    monkeypatch.setenv("DRINKS_DATABASE", str(blocked))
    from beverage_feed.api import app

    try:
        with pytest.raises(RuntimeError, match="cannot open price feed database"):
            with TestClient(app, raise_server_exceptions=True):
                pass
    finally:
        blocked.parent.chmod(0o700)


def test_lifespan_migrates_an_empty_database(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    database = tmp_path / "fresh.sqlite"
    monkeypatch.setenv("DRINKS_DATABASE", str(database))
    from beverage_feed.api import app

    with TestClient(app):
        with closing(sqlite3.connect(database)) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

    assert {"price_observations", "collection_runs", "retailers"} <= tables


# --- raw, no-frills endpoints -----------------------------------------------


def _insert_unmapped_result(database: Path, run_id: str, catalog_id: str) -> None:
    """Simulate a scraped-but-unmapped cell exactly as collect_run records it."""
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error, recorded_at)
            VALUES (?, ?, 'tesco', 'unmapped', 'no catalog mapping configured', ?)
            """,
            (run_id, catalog_id, "2026-01-01T00:00:00Z"),
        )
        connection.commit()


def test_results_includes_unmapped_rows_with_reasons(client):
    _insert_unmapped_result(
        Path(client.app.state.database), "run-raw-1", PACK.catalog_id
    )
    body = client.get("/results").json()

    statuses = {row["status"] for row in body}
    assert {"observed", "unmapped"} <= statuses
    unmapped = next(row for row in body if row["status"] == "unmapped")
    assert unmapped["error"] == "no catalog mapping configured"
    assert unmapped["pack_name"] == PACK.name


def test_results_filter_by_status_and_retailer(client):
    _insert_unmapped_result(
        Path(client.app.state.database), "run-raw-2", PACK.catalog_id
    )
    assert client.get("/results", params={"status": "unmapped"}).json() != []
    assert client.get("/results", params={"status": "source_error"}).json() == []
    tesco_only = client.get("/results", params={"retailer": "tesco"}).json()
    assert {row["retailer"] for row in tesco_only} == {"tesco"}


def test_runs_returns_recent_runs_with_parsed_summary(client):
    body = client.get("/runs").json()

    assert len(body) >= 1
    run = body[0]
    assert set(run) >= {"run_id", "started_at", "status", "summary"}
    assert isinstance(run["summary"], dict)


def test_candidates_returns_raw_scraped_listings(client):
    database = Path(client.app.state.database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO catalog_candidates (
                candidate_id, retailer, source_product_reference,
                source_item_id, source_product_name, displayed_price,
                raw_record, status, first_seen_at
            ) VALUES (?, 'dunnes', 'ref-1', 'item-1', 'Diet Coke 500ml',
                      '2.15', '{}', 'pending_review', '2026-01-01T00:00:00Z')
            """,
            ("test-candidate-1",),
        )
        connection.commit()

    body = client.get("/candidates").json()
    assert len(body) == 1
    assert body[0]["source_product_name"] == "Diet Coke 500ml"
    assert body[0]["status"] == "pending_review"


def test_health_reports_all_table_counts(client):
    _insert_unmapped_result(
        Path(client.app.state.database), "run-raw-3", PACK.catalog_id
    )
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["observations"] == 1
    assert body["collection_results"] == 2
    assert body["approved_mappings"] == 1
    assert body["code_mtime"]  # staleness signal for stale-container debugging


def _seed_consumer_scenario(database: Path) -> None:
    """Seed one pack exercising every consumer cell state."""
    with closing(sqlite3.connect(database)) as connection:
        connection.executemany(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count,
                 failed_count, summary)
            VALUES (?, ?, ?, 'ok', 0, 0, '{}')
            """,
            [
                ("run-a", "2026-01-01T10:00:00Z", "2026-01-01T10:01:00Z"),
                ("run-b", "2026-01-02T10:00:00Z", "2026-01-02T10:01:00Z"),
            ],
        )
        connection.execute(
            """
            INSERT INTO catalog_packs
                (catalog_id, name, brand, variant, pack_count, unit_size_ml,
                 package_type, search_term)
            VALUES ('cola-330', 'Cola 330ml Can', 'Cola', 'Original', 1, 330,
                    'can', 'Cola Original')
            """
        )
        mappings = [
            ("cola-330", "tesco", "approved"),
            ("cola-330", "dunnes", "approved"),
            ("cola-330", "supervalu", "approved"),
            ("cola-330", "lidl", "approved"),
            ("cola-330", "aldi", "dormant"),
        ]
        connection.executemany(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name, status)
            VALUES (?, ?, 'Cola 330ml Can', ?)
            """,
            mappings,
        )
        results = [
            # tesco: latest result observed → observed cell (is_best)
            ("run-b", "cola-330", "tesco", "observed", "2026-01-02T10:00:30Z"),
            # dunnes: latest result source_error → temporarily_unavailable
            ("run-b", "cola-330", "dunnes", "source_error", "2026-01-02T10:00:30Z"),
            # lidl: latest result not_found after an older observation → last_seen
            ("run-a", "cola-330", "lidl", "observed", "2026-01-01T10:00:30Z"),
            ("run-b", "cola-330", "lidl", "not_found", "2026-01-02T10:00:30Z"),
            # supervalu: approved, never any result → awaiting_price
        ]
        connection.executemany(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            results,
        )
        observations = [
            ("run-b", "cola-330", "tesco", "2.10", "2026-01-02T10:00:45Z"),
            ("run-a", "cola-330", "lidl", "1.95", "2026-01-01T10:00:45Z"),
        ]
        connection.executemany(
            """
            INSERT INTO price_observations
                (run_id, catalog_id, retailer, source_product_reference,
                 source_item_id, source_product_name, displayed_price, currency,
                 pack_count, unit_size_ml, package_type, observed_at)
            VALUES (?, ?, ?, 'ref', 'item', 'Cola 330ml Can', ?, 'EUR',
                    1, 330, 'can', ?)
            """,
            observations,
        )
        connection.commit()


def test_consumer_feed_covers_all_five_states(client):
    database = Path(client.app.state.database)
    _seed_consumer_scenario(database)
    body = client.get("/consumer/feed").json()

    assert body["standing_rule"] == "A missing price is not a stock or retirement claim."
    assert body["pack_count"] == 2
    pack = next(p for p in body["packs"] if p["catalog_id"] == "cola-330")
    cells = {c["retailer"]: c for c in pack["retailers"]}

    # aldi is dormant → omitted entirely, never a synthetic slot
    assert "aldi" not in cells

    tesco = cells["tesco"]
    assert tesco["state"] == "observed"
    assert tesco["displayed_price"] == "2.10"
    assert tesco["is_best"] is True
    assert tesco["currency"] == "EUR"

    assert cells["dunnes"]["state"] == "temporarily_unavailable"
    assert cells["dunnes"]["displayed_price"] is None

    assert cells["supervalu"]["state"] == "awaiting_price"

    lidl = cells["lidl"]
    assert lidl["state"] == "last_seen"
    assert lidl["displayed_price"] is None  # old price never shown as current
    assert lidl["last_seen_at"] == "2026-01-01T10:00:45Z"

    assert tesco["is_best"] is True
    assert all(c["is_best"] is False for r, c in cells.items() if r != "tesco")


def test_consumer_feed_shows_unmapped_retailer_as_not_available(client):
    # The fixture pack only has an approved aldi mapping; the other four
    # retailers must appear as not_available slots, not vanish.
    body = client.get("/consumer/feed").json()

    assert body["pack_count"] == 1
    cells = {c["retailer"]: c for c in body["packs"][0]["retailers"]}
    assert cells["aldi"]["state"] == "observed"
    assert cells["tesco"]["state"] == "not_available"
    assert cells["dunnes"]["state"] == "not_available"


def test_consumer_feed_filters_by_catalog_id(client):
    database = Path(client.app.state.database)
    _seed_consumer_scenario(database)

    body = client.get("/consumer/feed", params={"catalog_id": "cola-330"}).json()
    assert [p["catalog_id"] for p in body["packs"]] == ["cola-330"]

    empty = client.get(
        "/consumer/feed", params={"catalog_id": "does-not-exist"}
    ).json()
    assert empty["packs"] == []
    assert empty["pack_count"] == 0


def _seed_pack_with_mapping(
    database: Path,
    *,
    catalog_id: str,
    retailer: str,
    mapping_status: str,
) -> None:
    """Insert a catalog pack with a single mapping in the given status."""
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO catalog_packs
                (catalog_id, name, brand, variant, pack_count, unit_size_ml,
                 package_type, search_term)
            VALUES (?, ?, 'Brand', 'Variant', 1, 330, 'can', 'Search')
            """,
            (catalog_id, f"Pack {catalog_id}"),
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name, status)
            VALUES (?, ?, ?, ?)
            """,
            (catalog_id, retailer, f"Pack {catalog_id}", mapping_status),
        )
        connection.commit()


def _seed_inconclusive_scenario(database: Path) -> None:
    """Seed two approved-mapping packs whose latest result is inconclusive.

    ``fanta-can`` has an older Price Observation; ``sprite-can`` was never
    observed. Commit 911a2a5 introduced ``inconclusive`` for truncated pages:
    absence from a truncated page proves nothing, so neither pack may surface
    as unavailable or current.
    """
    with closing(sqlite3.connect(database)) as connection:
        connection.executemany(
            """
            INSERT INTO catalog_packs
                (catalog_id, name, brand, variant, pack_count, unit_size_ml,
                 package_type, search_term)
            VALUES (?, ?, ?, 'Original', 1, 330, 'can', ?)
            """,
            [
                ("fanta-can", "Fanta 330ml Can", "Fanta", "Fanta Original"),
                ("sprite-can", "Sprite 330ml Can", "Sprite", "Sprite Original"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name, status)
            VALUES (?, 'tesco', ?, 'approved')
            """,
            [("fanta-can", "Fanta 330ml Can"), ("sprite-can", "Sprite 330ml Can")],
        )
        connection.execute(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count,
                 failed_count, summary)
            VALUES ('run-inc', '2026-01-03T10:00:00Z', '2026-01-03T10:01:00Z',
                    'ok', 0, 0, '{}')
            """
        )
        connection.executemany(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, recorded_at)
            VALUES ('run-inc', ?, 'tesco', 'inconclusive', '2026-01-03T10:00:30Z')
            """,
            [("fanta-can",), ("sprite-can",)],
        )
        connection.execute(
            """
            INSERT INTO price_observations
                (run_id, catalog_id, retailer, source_product_reference,
                 source_item_id, source_product_name, displayed_price, currency,
                 pack_count, unit_size_ml, package_type, observed_at)
            VALUES ('run-a', 'fanta-can', 'tesco', 'ref', 'item',
                    'Fanta 330ml Can', '1.80', 'EUR', 1, 330, 'can',
                    '2026-01-01T10:00:45Z')
            """
        )
        connection.commit()


def test_consumer_feed_inconclusive_result_after_observation_stays_last_seen(client):
    """An inconclusive latest result never fakes current or unavailable.

    A truncated retailer page proves nothing about absence, so a pack with an
    older observation stays exactly where the last proof left it: last_seen,
    with the old price withheld.
    """
    database = Path(client.app.state.database)
    _seed_inconclusive_scenario(database)
    body = client.get("/consumer/feed").json()

    pack = next(p for p in body["packs"] if p["catalog_id"] == "fanta-can")
    tesco = next(c for c in pack["retailers"] if c["retailer"] == "tesco")

    assert tesco["state"] == "last_seen"
    assert tesco["label"] == "Last seen"
    assert tesco["displayed_price"] is None  # old price never shown as current
    assert tesco["last_seen_at"] == "2026-01-01T10:00:45Z"
    assert tesco["is_best"] is False


def test_consumer_feed_inconclusive_result_without_observation_is_awaiting_price(client):
    """Inconclusive on a never-observed pack stays awaiting_price.

    ``temporarily_unavailable`` means the latest result errored; an
    inconclusive page is not an error claim, and the approved mapping has
    genuinely never been observed. ``awaiting_price`` implies no availability.
    """
    database = Path(client.app.state.database)
    _seed_inconclusive_scenario(database)
    body = client.get("/consumer/feed").json()

    pack = next(p for p in body["packs"] if p["catalog_id"] == "sprite-can")
    tesco = next(c for c in pack["retailers"] if c["retailer"] == "tesco")

    assert tesco["state"] == "awaiting_price"
    assert tesco["displayed_price"] is None


def _seed_multipack_with_money_fields(database: Path) -> None:
    """Seed a 6-pack observed with Clubcard Price and DRS Deposit recorded."""
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO catalog_packs
                (catalog_id, name, brand, variant, pack_count, unit_size_ml,
                 package_type, search_term)
            VALUES ('cola-6pk', 'Cola 6x500ml', 'Cola', 'Original', 6, 500,
                    'bottle', 'Cola Original 6x')
            """
        )
        connection.execute(
            """
            INSERT INTO catalog_mappings
                (catalog_id, retailer, expected_product_name, status)
            VALUES ('cola-6pk', 'tesco', 'Cola 6x500ml', 'approved')
            """
        )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, recorded_at)
            VALUES ('run-m', 'cola-6pk', 'tesco', 'observed', '2026-01-02T10:00:30Z')
            """
        )
        connection.execute(
            """
            INSERT INTO price_observations
                (run_id, catalog_id, retailer, source_product_reference,
                 source_item_id, source_product_name, displayed_price,
                 clubcard_price, drs_deposit, currency, pack_count,
                 unit_size_ml, package_type, observed_at)
            VALUES ('run-m', 'cola-6pk', 'tesco', 'ref', 'item', 'Cola 6x500ml',
                    '8.40', '7.50', '0.72', 'EUR', 6, 500, 'bottle',
                    '2026-01-02T10:00:45Z')
            """
        )
        connection.commit()


def test_consumer_feed_money_dates_and_slot_shape_follow_spec_contract(client):
    """Spec §4 shapes: decimal-string money + currency, ISO-8601 UTC
    observed_at, and a slot carrying no operator diagnostics."""
    database = Path(client.app.state.database)
    _seed_multipack_with_money_fields(database)
    body = client.get("/consumer/feed").json()

    pack = next(p for p in body["packs"] if p["catalog_id"] == "cola-6pk")
    tesco = next(c for c in pack["retailers"] if c["retailer"] == "tesco")

    assert tesco["state"] == "observed"
    assert tesco["displayed_price"] == "8.40"
    assert tesco["clubcard_price"] == "7.50"
    assert tesco["drs_deposit"] == "0.72"  # own field, never folded into price
    assert tesco["component_unit_price"] == "1.40"  # derived from pack_count
    assert tesco["currency"] == "EUR"

    observed_at = tesco["observed_at"]
    assert observed_at.endswith("Z")
    datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")  # ISO-8601 UTC

    assert set(tesco) == {
        "retailer", "display_name", "state", "label", "displayed_price",
        "clubcard_price", "drs_deposit", "component_unit_price",
        "source_scope", "observed_at", "currency", "is_best",
    }


def test_consumer_feed_leaks_no_operator_diagnostics_anywhere(client):
    """Run ids, source refs, errors, and raw candidates stay operator-only."""
    database = Path(client.app.state.database)
    _seed_consumer_scenario(database)
    _seed_multipack_with_money_fields(database)
    body = client.get("/consumer/feed").json()

    forbidden_keys = {
        "run_id", "error", "source_item_id", "source_product_reference",
        "source_product_name", "raw_record", "candidate_id", "status",
    }

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            assert not forbidden_keys & set(node), forbidden_keys & set(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(body)
    serialized = json.dumps(body)
    assert "run-a" not in serialized and "run-b" not in serialized


def test_consumer_feed_omits_packs_without_any_approved_mapping(client):
    """Per-catalog-pack entries exist only where an approved mapping does."""
    database = Path(client.app.state.database)
    _seed_pack_with_mapping(
        database, catalog_id="review-only", retailer="tesco",
        mapping_status="review",
    )
    body = client.get("/consumer/feed").json()

    assert "review-only" not in {p["catalog_id"] for p in body["packs"]}

    filtered = client.get(
        "/consumer/feed", params={"catalog_id": "review-only"}
    ).json()
    assert filtered["packs"] == []
    assert filtered["pack_count"] == 0


def test_runs_survives_a_corrupt_summary_without_a_500(client):
    """A malformed run summary degrades to the raw string, never an error."""
    from beverage_feed.collector import ensure_schema

    database = Path(client.app.state.database)
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-other", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
             "failed", 0, 1, "{}"),
        )
        connection.execute(
            "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-bad", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
             "failed", 0, 1, "{not json"),
        )
        connection.commit()

    response = client.get("/runs")
    assert response.status_code == 200
    runs = response.json()
    bad = next(run for run in runs if run["run_id"] == "run-bad")
    assert bad["summary"] == "{not json"


def test_results_filter_by_run_id(client):
    from beverage_feed.collector import ensure_schema

    database = Path(client.app.state.database)
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.execute(
            "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-other", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z",
             "failed", 0, 1, "{}"),
        )
        connection.execute(
            "INSERT INTO collection_results "
            "(run_id, catalog_id, retailer, status, error, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("run-other", PACK.catalog_id, "tesco", "not_found",
             "no catalog mapping configured", "2026-01-01T00:00:00Z"),
        )
        connection.commit()

    matching = client.get("/results", params={"run_id": "run-other"}).json()
    assert [row["run_id"] for row in matching] == ["run-other"]
    everything = client.get("/results").json()
    assert {"run-other"} <= {row["run_id"] for row in everything}
