"""Read-only HTTP API coverage against temporary SQLite databases."""

import sqlite3
from contextlib import closing
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
