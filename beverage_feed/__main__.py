import sys

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

from .collector import main

raise SystemExit(main(argv))
