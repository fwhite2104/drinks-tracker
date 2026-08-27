"""Subcommand dispatch contract of ``python -m beverage_feed``.

Each subcommand must reach its module CLI and answer ``--help`` with exit
code 0, proving the dispatcher in ``__main__.py`` routes every command.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from beverage_feed import __file__ as package_init

SUBCOMMANDS = [
    "discovery",
    "review",
    "report",
    "basketwatch",
    "trace",
    "dashboard",
]


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return Path(package_init).parent.parent


@pytest.mark.parametrize("command", SUBCOMMANDS)
def test_every_subcommand_dispatches_and_answers_help(repo_root: Path, command: str):
    proc = subprocess.run(
        [sys.executable, "-m", "beverage_feed", command, "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_default_dispatch_reaches_the_collection_cli(repo_root: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "beverage_feed", "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in (proc.stdout + proc.stderr).lower()
