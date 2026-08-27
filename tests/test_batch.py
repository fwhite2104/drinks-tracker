"""Collection batch export/ingest coverage (CI egress routing)."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from beverage_feed.batch import (
    BATCH_VERSION,
    export_batch,
    ingest_batch,
)
from beverage_feed.collector import (
    AldiMapping,
    BenchmarkPack,
    collect_aldi_one,
    ensure_schema,
)

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


def _collected_database(tmp_path: Path, run_id: str) -> Path:
    database = tmp_path / "source.sqlite"
    record = {
        "productId": "000000000000336021",
        "name": "Still Water",
        "brand": "COMERAGH",
        "price": "€1.45",
    }
    # A caller-supplied run id owns the collection_runs row (the batch flow's
    # contract); create it up front so the FK is satisfied.
    with closing(sqlite3.connect(database)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count,
                 failed_count, summary)
            VALUES (?, ?, ?, 'completed', 1, 0, '{}')
            """,
            (run_id, run_id, run_id),
        )
        connection.commit()
    collect_aldi_one(
        PACK,
        AldiMapping(catalog_id=PACK.catalog_id, expected_product_name="Still Water"),
        lambda _: {"items": [record]},
        database,
        _run_id=run_id,
    )
    return database


def _counts(database: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "collection_runs",
                "collection_results",
                "price_observations",
            )
        }


def test_export_then_ingest_round_trips_observation(tmp_path):
    source = _collected_database(tmp_path, "run-ci-1")
    target = tmp_path / "feed.sqlite"

    batch = export_batch(source, "run-ci-1")

    assert batch["batch_version"] == BATCH_VERSION
    assert batch["run_id"] == "run-ci-1"
    assert len(batch["collection_runs"]) == 1
    assert len(batch["price_observations"]) == 1
    assert batch["price_observations"][0]["displayed_price"] == "1.45"

    summary = ingest_batch(target, json.loads(json.dumps(batch)))

    assert summary["status"] == "ingested"
    assert summary["price_observations"] == 1
    with closing(sqlite3.connect(target)) as connection:
        row = connection.execute(
            """
            SELECT po.retailer, po.catalog_id, po.displayed_price, cr.status
            FROM price_observations po
            JOIN collection_results cr
              ON cr.run_id = po.run_id
             AND cr.catalog_id = po.catalog_id
             AND cr.retailer = po.retailer
            """
        ).fetchone()
    assert row == ("aldi", PACK.catalog_id, "1.45", "observed")


def test_ingest_is_idempotent_for_the_same_batch(tmp_path):
    source = _collected_database(tmp_path, "run-ci-2")
    target = tmp_path / "feed.sqlite"
    batch = export_batch(source, "run-ci-2")

    first = ingest_batch(target, batch)
    second = ingest_batch(target, batch)

    assert first["status"] == "ingested"
    assert second["status"] == "skipped"
    assert second["reason"] == "run already present"
    assert _counts(target) == _counts(source)


def test_ingest_rejects_unsupported_batch_version(tmp_path):
    target = tmp_path / "feed.sqlite"
    batch = {"batch_version": 99, "run_id": "run-x", "collection_runs": [{}]}

    try:
        ingest_batch(target, batch)
    except ValueError as exc:
        assert "unsupported batch version" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_ingest_rejects_batch_without_run_rows(tmp_path):
    target = tmp_path / "feed.sqlite"
    batch = {"batch_version": BATCH_VERSION, "run_id": "run-x", "collection_runs": []}

    try:
        ingest_batch(target, batch)
    except ValueError as exc:
        assert "no collection_runs" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_export_fails_for_unknown_run(tmp_path):
    source = _collected_database(tmp_path, "run-ci-3")

    try:
        export_batch(source, "does-not-exist")
    except ValueError as exc:
        assert "run not found" in str(exc)
    else:
        raise AssertionError("expected ValueError")
