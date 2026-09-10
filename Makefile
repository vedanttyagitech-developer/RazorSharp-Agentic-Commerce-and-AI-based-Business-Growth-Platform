# The short list of things a person actually runs.
#
# Order of a first run on a clean machine:
#
#     make bootstrap     databases, migrations, restricted roles -- and proof they are restricted
#     make seed          one demo tenant and merchant in commerce_dev
#     make demo          the API and the Action Executor, together
#     make web           the buyer storefront, in another terminal
#
# See docs/DEMO.md for the recording runbook and the eleven demonstration steps.

SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help bootstrap seed test lint types gate api executor action-executor worker web demo clean

# uv installs under the user's home; nothing here assumes whose. `--no-sync` because the
# environment is resolved once, deliberately, and never mutated by running a command.
UV := PATH="$(HOME)/.local/bin:$$PATH" uv run --no-sync

# Every package's source tree. Expanded at parse time, so a new package is picked up the
# next time make runs rather than needing this list edited.
SRC := $(wildcard packages/*/src)

help:  ## list these targets
	@printf '\n\033[1mGoverned Agentic Commerce\033[0m -- make targets\n\n'; grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'; printf '\nFirst run: make bootstrap && make seed && make demo\nRunbook:   docs/DEMO.md\n\n'

bootstrap:  ## create both databases, migrate them, install and verify the restricted roles
	@bash scripts/bootstrap_local.sh

seed:  ## seed the demo tenant and merchant into commerce_dev (idempotent)
	@$(UV) python scripts/seed_demo_tenant.py

test:  ## run the backend suites against commerce_test; a skipped db suite is a failure
	@REQUIRE_DB=1 $(UV) python scripts/test_packages.py -q -m 'not voice_live and not razorpay_live'

lint:  ## ruff check, then ruff format --check
	@$(UV) ruff check packages scripts conftest.py && $(UV) ruff format --check packages scripts conftest.py

types:  ## mypy --strict over every package source tree and the scripts
	@$(UV) mypy $(SRC) scripts

gate: lint types test  ## lint, then types, then tests -- the order that fails fastest

api:  ## run the API alone on :8000
	@bash scripts/run_demo.sh --api-only

web:  ## run the current buyer and merchant copilot frontend
	@cd apps/razorsharp-concept && npm run dev -- --port 3000

executor:  ## run the Action Executor alone
	@bash scripts/run_demo.sh --worker-only

action-executor: executor  ## alias for executor

worker: executor  ## alias for backwards compatibility

demo:  ## run the API and the Action Executor together; Ctrl-C stops both
	@bash scripts/run_demo.sh

clean:  ## remove build and test caches; never touches .venv, node_modules or any database
	@find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} + ; rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis test-results ; echo "caches removed"
