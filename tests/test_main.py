"""Subcommand dispatch contract of ``python -m beverage_feed``.

The subcommand matrix is derived from the dispatch literals in
``__main__.py`` at test time, so every routed command is covered — the
hand-picked list in the original missed routes when subcommands shipped.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from beverage_feed import __file__ as package_init

REPO_ROOT = Path(package_init).parent.parent
MAIN_SOURCE = (REPO_ROOT / "beverage_feed" / "__main__.py").read_text()


def _dispatch_literals() -> list[str]:
    """Extract every ``argv[0] == "..."`` subcommand string from __main__.py."""
    literals: list[str] = []
    for node in ast.parse(MAIN_SOURCE).body:
        if not isinstance(node, ast.If):
            continue
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                literals.append(sub.value)
    return literals


def _load_env_file_source() -> str:
    """The ``_load_env_file`` def, isolated so importing doesn't dispatch."""
    for node in ast.parse(MAIN_SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == "_load_env_file":
            return textwrap.dedent(ast.get_source_segment(MAIN_SOURCE, node) or "")
    raise AssertionError("_load_env_file not found in __main__.py")


def _compile_load_env_file():
    namespace: dict[str, object] = {"os": os, "Path": Path}
    exec(compile(_load_env_file_source(), "__main__.py", "exec"), namespace)
    return namespace["_load_env_file"]


SUBCOMMANDS = _dispatch_literals()


def test_dispatch_literals_are_found():
    # Guard the derivation itself against __main__.py rewrites.
    assert {"trace", "discovery", "canary"}.issubset(set(SUBCOMMANDS)), SUBCOMMANDS


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_routed_subcommand_answers_help_with_exit_zero(command: str):
    proc = subprocess.run(
        [sys.executable, "-m", "beverage_feed", command, "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_default_dispatch_reaches_the_collection_cli_help():
    proc = subprocess.run(
        [sys.executable, "-m", "beverage_feed", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_trace_with_a_missing_database_fails_cleanly_with_exit_2(tmp_path: Path):
    proc = subprocess.run(
        [
            sys.executable, "-m", "beverage_feed", "trace",
            "--database", str(tmp_path / "absent.sqlite"),
            "--catalog-id", "water-5l",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert "database not found" in proc.stdout


def test_load_env_file_does_not_override_already_set_variables(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("DRINKS_DATABASE=data/feed.sqlite\nOTHER=from-file\n")
    os.environ["DRINKS_DATABASE"] = "preset.sqlite"
    try:
        _compile_load_env_file()(env_file)

        assert os.environ["DRINKS_DATABASE"] == "preset.sqlite"
        assert os.environ["OTHER"] == "from-file"
    finally:
        os.environ.pop("DRINKS_DATABASE", None)
        os.environ.pop("OTHER", None)


def test_load_env_file_sets_unset_variables_and_strips_quotes(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text('API_TEST="quoted value"\n')
    os.environ.pop("API_TEST", None)
    try:
        _compile_load_env_file()(env_file)

        assert os.environ["API_TEST"] == "quoted value"
    finally:
        os.environ.pop("API_TEST", None)
