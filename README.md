# Drinks Tracker

Local collection and review tools for the Irish beverage price feed.

## Development

Use the project-local virtual environment, then install the pinned development
checks:

```sh
.venv/bin/python -m pip install -e '.[dev]'
```

Run the complete local check suite with one command:

```sh
.venv/bin/python -m pytest
```

The suite uses captured fixtures and test doubles; it never calls live retailer
endpoints. The command fails when no tests are discovered.
