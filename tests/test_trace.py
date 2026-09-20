"""Coverage for the read-only pipeline tracer (``python -m beverage_feed trace``)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from beverage_feed.collector import AldiMapping, collect_aldi_one
from beverage_feed.trace import main, trace


PACK_ARGS = dict(
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
def database(tmp_path: Path) -> Path:
    """A database with one observed cell and one scraped-but-unmapped result."""
    from beverage_feed.collector import BenchmarkPack

    pack = BenchmarkPack(**PACK_ARGS)
    database = tmp_path / "feed.sqlite"
    collect_aldi_one(
        pack,
        AldiMapping(catalog_id=pack.catalog_id, expected_product_name="Still Water"),
        lambda _: {
            "items": [
                {
                    "productId": "336021",
                    "name": "Still Water",
                    "brand": "COMERAGH",
                    "price": "\u20ac1.45",
                }
            ]
        },
        database,
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error, recorded_at)
            VALUES ('run-x', ?, 'tesco', 'unmapped',
                    'no catalog mapping configured', '2026-01-01T00:00:00Z')
            """,
            (pack.catalog_id,),
        )
        (run_id,) = connection.execute(
            "SELECT run_id FROM collection_runs LIMIT 1"
        ).fetchone()
        connection.execute(
            """
            INSERT INTO collection_diagnostics
                (run_id, retailer, catalog_id, level, event, message, created_at)
            VALUES (?, 'aldi', ?, 'warning', 'drs_not_available',
                    'aldi offer exposes no DRS deposit', '2026-01-01T00:00:00Z')
            """,
            (run_id, pack.catalog_id),
        )
        connection.commit()
    return database


def test_trace_reports_every_stage_for_a_catalog_id(database: Path, capsys):
    exit_code = trace(database, catalog_id=PACK_ARGS["catalog_id"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"catalog_id={PACK_ARGS['catalog_id']}" in output
    assert "mappings" in output and "approved" in output
    assert "recent collection results" in output
    assert "status=observed" in output
    assert "status=unmapped" in output
    assert "no catalog mapping configured" in output
    assert "price observations" in output
    assert "displayed_price=1.45" in output


def test_trace_filters_by_retailer(database: Path, capsys):
    exit_code = trace(
        database, catalog_id=PACK_ARGS["catalog_id"], retailer="tesco"
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status=unmapped" in output
    assert "status=observed" not in output


def test_trace_resolves_reference_to_candidates_when_no_cell_known(
    database: Path, capsys
):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO catalog_candidates (
                candidate_id, retailer, source_product_reference,
                source_item_id, source_product_name, displayed_price,
                raw_record, status, first_seen_at
            ) VALUES ('dunnes:ref-1:item-1', 'dunnes', 'ref-1', 'item-1',
                      'Diet Coke 500ml', '2.15', '{}', 'pending_review',
                      '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()

    exit_code = trace(database, reference="ref-1")

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Diet Coke 500ml" in output


def test_trace_unknown_reference_fails_cleanly(database: Path, capsys):
    exit_code = trace(database, reference="does-not-exist")

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "may only exist in catalog_candidates" in output


def test_main_requires_a_catalog_id_or_reference():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_main_returns_2_when_the_database_is_missing(tmp_path: Path):
    exit_code = main([
        "--database", str(tmp_path / "absent.sqlite"),
        "--catalog-id", "water-5l",
    ])
    assert exit_code == 2


def test_main_returns_2_when_the_database_is_not_sqlite(tmp_path: Path):
    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"definitely not a database")
    exit_code = main(["--database", str(corrupt), "--catalog-id", "water-5l"])
    assert exit_code == 2


def test_main_traces_successfully_and_exits_zero(database: Path):
    exit_code = main(["--database", str(database), "--catalog-id", "water-5l"])
    assert exit_code == 0


def test_trace_reports_collection_diagnostics(database: Path, capsys):
    exit_code = trace(database, catalog_id=PACK_ARGS["catalog_id"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "collection diagnostics" in output
    assert "level=warning" in output
    assert "aldi offer exposes no DRS deposit" in output


def test_trace_resolves_by_source_item_id_not_only_reference(database: Path, capsys):
    # The aldi observation carries source_item_id from the collected listing;
    # resolve the cell from it and show the observed stage.
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        (observed_id,) = connection.execute(
            "SELECT source_item_id FROM collection_results"
            " WHERE status='observed'"
            " AND source_item_id IS NOT NULL LIMIT 1"
        ).fetchone()
    assert observed_id, "fixture must contain an observed source_item_id"

    exit_code = trace(database, reference=observed_id)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status=observed" in output


def test_trace_matches_candidates_by_candidate_id(database: Path, capsys):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO collection_runs VALUES
                ('run-y', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z',
                 'completed', 0, 0, '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error,
                 source_product_reference, source_item_id, recorded_at)
            VALUES ('run-y', ?, 'tesco', 'unmapped',
                    'no catalog mapping configured', 'cand-77', 'cand-77',
                    '2026-01-01T00:00:00Z')
            """,
            (PACK_ARGS["catalog_id"],),
        )
        connection.execute(
            """
            INSERT INTO catalog_candidates (
                candidate_id, retailer, source_product_reference,
                source_item_id, source_product_name, displayed_price,
                raw_record, status, first_seen_at
            ) VALUES ('tesco:cand-77', 'tesco', 'ref-x', 'cand-77',
                      'Pepsi Max 2L', '3.00', '{}', 'pending_review',
                      '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()

    exit_code = trace(database, reference="cand-77")

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Pepsi Max 2L" in output
    assert "tesco:cand-77" in output
