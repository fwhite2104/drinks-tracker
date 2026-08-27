.PHONY: build up down collect discover review report serve test coverage ingest-basketwatch dashboard

build:
	docker compose build

up: build
	docker compose up -d

down:
	docker compose down

collect:
	docker compose run --rm collector python -m beverage_feed $(ARGS)

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
