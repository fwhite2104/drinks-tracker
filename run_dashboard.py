#!/usr/bin/env python3
"""Desktop-style launcher for the local Operator Dashboard.

Usage:
    .venv/bin/python run_dashboard.py
    .venv/bin/python run_dashboard.py --sprint          # review-sprint prototype (ticket 05)
    .venv/bin/python -m beverage_feed dashboard --no-browser
"""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--sprint" in args:
        from beverage_feed.dashboard_sprint import main as sprint_main

        return sprint_main([arg for arg in args if arg != "--sprint"])
    from beverage_feed.dashboard import main as dashboard_main

    return dashboard_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
