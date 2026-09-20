"""Collection batch export/ingest coverage (CI egress routing)."""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from beverage_feed import batch as batch_module
from beverage_feed.batch import (
    BATCH_VERSION,
    export_batch,
    ingest_batch,
    pull_latest_batch,
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
    # Prove re-ingest is a genuine no-op by run identity, not merely an
    # early return: a local mutation of persisted rows must survive intact.
    with closing(sqlite3.connect(target)) as connection:
        connection.execute(
            "UPDATE price_observations SET displayed_price = '9.99'"
        )
        connection.commit()
    second = ingest_batch(target, batch)

    assert first["status"] == "ingested"
    assert second["status"] == "skipped"
    assert second["reason"] == "run already present"
    with closing(sqlite3.connect(target)) as connection:
        mutated = connection.execute(
            "SELECT displayed_price FROM price_observations"
        ).fetchone()[0]
    assert mutated == "9.99"


def test_source_error_results_round_trip_verbatim(tmp_path):
    """A partial run ships too: source_error is truthful data (data-model.md 60-62)."""
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        ensure_schema(connection)
        connection.execute(
            """
            INSERT INTO collection_runs
                (run_id, started_at, finished_at, status, observed_count,
                 failed_count, summary)
            VALUES ('run-ci-err', 'run-ci-err', 'run-ci-err', 'completed', 0, 1, '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO collection_results
                (run_id, catalog_id, retailer, status, error, recorded_at)
            VALUES ('run-ci-err', ?, 'tesco', 'source_error',
                    'Akamai 403: access denied', '2026-09-04T04:15:00Z')
            """,
            (PACK.catalog_id,),
        )
        connection.commit()

    batch = export_batch(source, "run-ci-err")
    target = tmp_path / "feed.sqlite"
    summary = ingest_batch(target, json.loads(json.dumps(batch)))

    assert summary["status"] == "ingested"
    assert summary["collection_results"] == 1
    assert summary["price_observations"] == 0
    with closing(sqlite3.connect(target)) as connection:
        row = connection.execute(
            """
            SELECT status, error FROM collection_results
            WHERE run_id = 'run-ci-err'
            """
        ).fetchone()
    assert row == ("source_error", "Akamai 403: access denied")


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


class PullLatestBatchTests(unittest.TestCase):
    """pull-batch: latest-successful artifact download + whole-run ingest.

    The GitHub API and artifact-zip download are stubbed at the module seams
    (``_api_get`` / ``_download_zip``); no network is ever touched.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _install(self, runs, artifacts, zip_bytes):
        """Stub the batch module's API seam; return (urls, patches)."""
        urls: list[str] = []

        def fake_api_get(url, headers):
            urls.append(url)
            if url.endswith("/artifacts"):
                return {"artifacts": artifacts}
            return {"workflow_runs": runs}

        def fake_download_zip(request):
            urls.append(request.full_url)
            return zip_bytes

        return (
            urls,
            patch.object(batch_module, "_api_get", fake_api_get),
            patch.object(batch_module, "_download_zip", fake_download_zip),
        )

    def _zip_of(self, batch_json: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("collection-batch.json", batch_json)
        return buffer.getvalue()

    def test_pull_ingests_the_latest_successful_run(self):
        root = Path(self._tmp.name)
        source = _collected_database(root, "run-ci-pull")
        target = root / "feed.sqlite"
        batch = export_batch(source, "run-ci-pull")
        urls, api_patch, zip_patch = self._install(
            runs=[{"id": 777}],
            artifacts=[{"id": 5, "name": "collection-batch", "expired": False}],
            zip_bytes=self._zip_of(json.dumps(batch).encode()),
        )

        with patch.dict(os.environ, {"DRINKS_DATABASE": str(target)}):
            with api_patch, zip_patch:
                summary = pull_latest_batch("org/repo", "token-abc")

        self.assertEqual(summary["status"], "ingested")
        self.assertEqual(summary["run_id"], "run-ci-pull")
        run_urls = [u for u in urls if "status=success" in u]
        self.assertTrue(
            any("status=success&per_page=1" in u for u in run_urls), urls
        )
        with closing(sqlite3.connect(target)) as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM collection_runs WHERE run_id='run-ci-pull'"
            ).fetchone()[0]
        self.assertEqual(runs, 1)

    def test_pull_rejects_artifacts_with_more_than_one_json_file(self):
        root = Path(self._tmp.name)
        source = _collected_database(root, "run-ci-multi")
        batch = export_batch(source, "run-ci-multi")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("collection-batch.json", json.dumps(batch))
            archive.writestr("extra.json", "{}")
        urls, api_patch, zip_patch = self._install(
            runs=[{"id": 888},],
            artifacts=[{"id": 9, "name": "collection-batch", "expired": False}],
            zip_bytes=buffer.getvalue(),
        )

        with patch.dict(os.environ, {"DRINKS_DATABASE": str(root / "feed.sqlite")}):
            with api_patch, zip_patch:
                with self.assertRaisesRegex(RuntimeError, "must contain one JSON"):
                    pull_latest_batch("org/repo", "token-abc")

    def test_pull_requires_a_github_token(self):
        with self.assertRaisesRegex(ValueError, "GITHUB_TOKEN"):
            pull_latest_batch("org/repo", "")


if __name__ == "__main__":
    unittest.main()
