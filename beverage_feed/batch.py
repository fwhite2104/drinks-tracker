"""Collection batch export/ingest for CI egress routing.

Retailers blocked at the edge for a home IP (Tesco behind Akamai) are
collected on GitHub Actions instead. The CI run writes its rows into a
disposable SQLite database; ``export-batch`` serialises the run's rows from
the three append-only tables (``collection_runs``, ``collection_results``,
``price_observations``) into one self-describing JSON batch, and
``ingest-batch`` inserts that batch into the live feed database.

Idempotency is whole-run: the batch is keyed by ``run_id`` and ingest is
insert-if-absent at run granularity — re-ingesting the same batch is a
no-op by construction. ``pull-batch`` closes the loop: it downloads the
latest successful batch artifact from GitHub Actions and ingests it, so the
VM (no inbound ports, Cloudflare Tunnel for ingress) pulls instead of
listening. A CI ``source_error`` batch is still truthful data: the Current
Feed's latest-result-wins rule handles downstream semantics with no
special-casing here.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .collector import ensure_schema, timestamp

BATCH_VERSION = 1
DEFAULT_REPOSITORY = "fwhite2104/drinks-tracker"
DEFAULT_WORKFLOW = "collect.yml"
DEFAULT_ARTIFACT = "collection-batch"

_TABLES = ("collection_runs", "collection_results", "price_observations")


def export_batch(database: str | Path, run_id: str) -> dict[str, Any]:
    """Serialise one collection run's rows from the three append-only tables."""
    if not str(run_id).strip():
        raise ValueError("run id must not be empty")
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        batch: dict[str, Any] = {
            "batch_version": BATCH_VERSION,
            "run_id": run_id,
            "exported_at": timestamp(),
        }
        for table in _TABLES:
            column = "run_id"
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {column} = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            batch[table] = [dict(row) for row in rows]
    if not batch["collection_runs"]:
        raise ValueError(f"run not found in database: {run_id}")
    return batch


def ingest_batch(database: str | Path, batch: dict[str, Any]) -> dict[str, Any]:
    """Insert a batch into the feed database; whole-run idempotent."""
    if batch.get("batch_version") != BATCH_VERSION:
        raise ValueError(f"unsupported batch version: {batch.get('batch_version')!r}")
    run_id = batch.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("batch has no run_id")
    runs = batch.get("collection_runs")
    if not isinstance(runs, list) or not runs:
        raise ValueError("batch has no collection_runs row")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_schema(connection)
        existing = connection.execute(
            "SELECT 1 FROM collection_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            return {
                "status": "skipped",
                "run_id": run_id,
                "reason": "run already present",
            }
        _insert_rows(connection, "collection_runs", runs)
        results = batch.get("collection_results") or []
        observations = batch.get("price_observations") or []
        _insert_rows(connection, "collection_results", results)
        _insert_rows(connection, "price_observations", observations)
        connection.commit()
    return {
        "status": "ingested",
        "run_id": run_id,
        "collection_results": len(results),
        "price_observations": len(observations),
    }


def _insert_rows(
    connection: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    columns = [column for column in rows[0] if column != "observation_id"]
    placeholders = ", ".join("?" for _ in columns)
    names = ", ".join(columns)
    payload = [
        tuple(row.get(column) for column in columns)
        for row in rows
        if isinstance(row, dict)
    ]
    connection.executemany(
        f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({placeholders})",
        payload,
    )


def pull_latest_batch(
    repository: str,
    token: str,
    *,
    workflow: str = DEFAULT_WORKFLOW,
    artifact: str = DEFAULT_ARTIFACT,
) -> dict[str, Any]:
    """Download the latest successful workflow batch artifact and ingest it.

    Uses the GitHub API with a fine-grained PAT (actions:read). The artifact
    zip must contain exactly one batch JSON file.
    """
    if not token:
        raise ValueError("a GitHub token is required; set GITHUB_TOKEN")
    api = f"https://api.github.com/repos/{repository}/actions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "drinks-tracker/0.1",
    }
    runs_response = _api_get(
        f"{api}/workflows/{workflow}/runs?status=success&per_page=1", headers
    )
    runs = runs_response.get("workflow_runs") or []
    if not runs:
        raise RuntimeError(f"no successful runs of {workflow} in {repository}")
    run_id = runs[0]["id"]
    artifacts_response = _api_get(f"{api}/runs/{run_id}/artifacts", headers)
    artifacts = artifacts_response.get("artifacts") or []
    match = next(
        (a for a in artifacts if a.get("name") == artifact and not a.get("expired")),
        None,
    )
    if match is None:
        raise RuntimeError(f"artifact {artifact!r} not found on run {run_id}")
    zip_request = Request(
        f"{api}/artifacts/{match['id']}/zip", headers=headers
    )
    archive = zipfile.ZipFile(io.BytesIO(_download_zip(zip_request)))
    names = [name for name in archive.namelist() if name.endswith(".json")]
    if len(names) != 1:
        raise RuntimeError(f"artifact {artifact!r} must contain one JSON batch")
    batch = json.loads(archive.read(names[0]))
    return ingest_batch(_batch_database(), batch)


class _DropAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects WITHOUT the Authorization header.

    Artifact downloads 302 to a short-lived signed URL on a different host;
    replaying the bearer token there makes Azure reject the request (401).
    The default handler strips auth on cross-host redirects only in some
    code paths — this guarantees it.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:  # noqa: ANN001
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers.pop("Authorization", None)
        return new


def _download_zip(request: Request) -> bytes:
    opener = urllib.request.build_opener(_DropAuthRedirectHandler)
    with opener.open(request, timeout=60) as response:
        return response.read()


def _api_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def _batch_database() -> str:
    database = os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")
    return database


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export one collection run as an ingestable JSON batch"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")),
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="batch output path (default: stdout)",
    )
    args = parser.parse_args(argv)
    batch = export_batch(args.database, args.run_id)
    text = json.dumps(batch, indent=2)
    if args.out is None:
        sys.stdout.write(text + "\n")
    else:
        args.out.write_text(text)
        print(f"batch written: {args.out} (run {args.run_id})")
    return 0


def ingest_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a collection batch (whole-run idempotent)"
    )
    parser.add_argument("batch", type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(os.environ.get("DRINKS_DATABASE", "data/feed.sqlite")),
    )
    args = parser.parse_args(argv)
    batch = json.loads(args.batch.read_text())
    summary = ingest_batch(args.database, batch)
    print(json.dumps(summary))
    return 0


def pull_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull the latest CI batch artifact from GitHub Actions and ingest it"
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY_REMOTE", DEFAULT_REPOSITORY),
    )
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    summary = pull_latest_batch(
        args.repository, token, workflow=args.workflow, artifact=args.artifact
    )
    print(json.dumps(summary))
    return 0
