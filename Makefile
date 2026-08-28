.PHONY: build up down collect canary discover review report serve test coverage ingest-basketwatch dashboard deploy-check

build:
	docker compose build

up: build
	docker compose up -d

down:
	docker compose down

collect:
	docker compose run --rm collector python -m beverage_feed $(ARGS)

# Manual live retailer canary (audit-10) — probes one known mapped listing
# per retailer, never scheduled, never run by tests or CI checks:
#   make canary
#   make canary ARGS="--retailer tesco"
# A failing canary (endpoint drift / product absence) means: do not trust the
# next feed run until it is fixed. Enables the release gate when collection
# runs with --release-gate / DRINKS_RELEASE_GATE=1.
canary:
	.venv/bin/python -m beverage_feed canary $(ARGS)

discover:
	docker compose run --rm discovery python -m beverage_feed discovery $(ARGS)

review:
	docker compose run --rm discovery python -m beverage_feed review $(ARGS)

report:
	docker compose run --rm discovery python -m beverage_feed report $(ARGS)

serve:
	docker compose up -d api

test:
	.venv/bin/python -m pytest

coverage:
	.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report

ingest-basketwatch:
	docker compose run --rm collector python -m beverage_feed basketwatch $(ARGS)

dashboard:
	.venv/bin/python -m beverage_feed dashboard $(ARGS)

# Verify the deployed API (pass the public URL from outside the LAN):
#   make deploy-check BASE_URL=https://api.<your-domain>
# See deploy/README.md §6. Defaults to the local LAN endpoint.
BASE_URL ?= http://localhost:8000
deploy-check:
	./deploy/healthcheck.sh $(BASE_URL)
