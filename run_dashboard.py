#!/usr/bin/env python3
"""Desktop-style launcher for the local Operator Dashboard.

Usage:
    .venv/bin/python run_dashboard.py
    .venv/bin/python -m beverage_feed dashboard --no-browser
"""

from __future__ import annotations

from beverage_feed.dashboard import main

if __name__ == "__main__":
    raise SystemExit(main())
