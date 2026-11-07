PYTHON ?= python

.PHONY: lint test services-up services-down dist

lint:
	$(PYTHON) -m pyflakes vidb tests

test:
	$(PYTHON) -m pytest -q

# The integration suites need these; the connection URIs are in the README.
services-up:
	docker compose up -d --wait

services-down:
	docker compose down -v

dist:
	$(PYTHON) -m build --outdir dist .
