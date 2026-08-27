"""Entry point for ``python -m beverage_feed``.

Loads git-ignored ``.env`` values into the environment before dispatching, so
local and cron invocations see the same credentials that docker-compose
supplies via its env_file. Values already set in the environment win.
"""

import os
import sys
from pathlib import Path


def _load_env_file(path: Path = Path(".env")) -> None:
    """Set unset environment variables from a dotenv file (stdlib only)."""
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()

argv = sys.argv[1:]
if argv and argv[0] == "discovery":
    from .discovery_run import main as discovery_main

    raise SystemExit(discovery_main(argv[1:]))
if argv and argv[0] == "review":
    from .discovery_cli import main as review_main

    raise SystemExit(review_main(argv[1:]))
if argv and argv[0] == "report":
    from .discovery_report import main as report_main

    raise SystemExit(report_main(argv[1:]))
if argv and argv[0] == "basketwatch":
    from .basketwatch import main as basketwatch_main

    raise SystemExit(basketwatch_main(argv[1:]))
if argv and argv[0] == "trace":
    from .trace import main as trace_main

    raise SystemExit(trace_main(argv[1:]))
if argv and argv[0] == "export-batch":
    from .batch import export_main as export_batch_main

    raise SystemExit(export_batch_main(argv[1:]))
if argv and argv[0] == "ingest-batch":
    from .batch import ingest_main as ingest_batch_main

    raise SystemExit(ingest_batch_main(argv[1:]))
if argv and argv[0] == "pull-batch":
    from .batch import pull_main as pull_batch_main

    raise SystemExit(pull_batch_main(argv[1:]))
if argv and argv[0] == "dashboard":
    from .dashboard import main as dashboard_main

    raise SystemExit(dashboard_main(argv[1:]))

from .collector import main

raise SystemExit(main(argv))
