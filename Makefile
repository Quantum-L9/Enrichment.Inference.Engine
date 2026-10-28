.PHONY: setup dev dev-build dev-down dev-clean test test-unit test-integration test-compliance test-ci test-contracts test-all test-watch lint lint-fix audit audit-strict audit-json verify agent-check agent-fix agent-full build prod prod-build prod-down prod-logs deploy clean push pr pr-base-guard pr-check pr-guards pr-evaluate pr-merge pr-merge-dry pr-ship pr-rerun

IMAGE_NAME ?= enrichment-api
SERVICE_NAME ?= enrichment-api
COMPOSE_FILE ?= docker-compose.prod.yml
COVERAGE_MIN ?= 71

PYTHON := $(shell if [ -x "$(CURDIR)/.venv/bin/python" ]; then printf '%s' "$(CURDIR)/.venv/bin/python"; else command -v python3; fi)
PYTEST := $(PYTHON) -m pytest

# ============================================================
# SETUP
# ============================================================
setup:
	pip install -e ".[dev]"
	pre-commit install

# ============================================================
# TESTING — TIERED
# ============================================================
test:  ## Quick: unit tests only
	$(PYTEST) tests/ -v --tb=short -x

test-unit:  ## Unit tests only
	$(PYTEST) tests/unit/ -v --tb=short

test-integration:  ## Integration tests (requires services)
	$(PYTEST) tests/integration/ -v --tb=short -m integration

test-compliance:  ## Architecture compliance tests
	$(PYTEST) tests/compliance/ -v --tb=short

test-ci:  ## CI-level tests (contract enforcement, loader tests)
	$(PYTEST) tests/ci/ -v --tb=short

test-contracts:  ## Repository contract call enforcement only
	$(PYTEST) tests/ci/test_repository_contract_calls.py -v --tb=short

test-all:  ## Full test suite with coverage
	ruff check .
	ruff format --check .
	mypy app
	$(PYTEST) tests/ -v --tb=short --cov=app --cov-report=term-missing --cov-fail-under=$(COVERAGE_MIN)

test-watch:  ## Watch mode for unit tests
	$(PYTEST) tests/unit/ -- -v --tb=short -w

# ============================================================
# QUALITY — LINT + TYPE CHECK
# ============================================================
lint:  ## Lint + format check + type check
	ruff check .
	ruff format --check .
	mypy app

lint-fix:  ## Auto-fix lint and format issues
	ruff check . --fix
	ruff format .

# ============================================================
# AUDIT — 27-RULE ENGINE
# ============================================================
audit:  ## Run 27-rule audit engine (informational)
	$(PYTHON) tools/audit_engine.py

audit-strict:  ## Run 27-rule audit engine (fail on CRITICAL/HIGH)
	$(PYTHON) tools/audit_engine.py --strict

audit-json:  ## Run 27-rule audit engine (JSON output)
	$(PYTHON) tools/audit_engine.py --json

# ============================================================
# CONTRACT VERIFICATION
# ============================================================
verify:  ## Verify contract manifest integrity
	$(PYTHON) tools/verify_contracts.py

# ============================================================
# AGENT WORKFLOW — THE UNIVERSAL GATES
# ============================================================
agent-check:  ## THE universal gate. Agents run this before every commit.
	@echo "╔══════════════════════════════════════════════╗"
	@echo "║  L9 Agent Check — Enrichment.Inference.Engine ║"
	@echo "╚══════════════════════════════════════════════╝"
	@echo ""
	@echo "=== [1/8] LINT ===" && ruff check .
	@echo "=== [2/8] FORMAT ===" && ruff format --check .
	@echo "=== [3/8] TYPES (ratchet, ADR-001) ===" && $(PYTHON) tools/mypy_ratchet.py
	@echo "=== [4/8] UNIT TESTS ===" && $(PYTEST) tests/unit/ tests/compliance/ -v --tb=short -x
	@echo "=== [5/8] CI TESTS ===" && $(PYTEST) tests/ci/ -v --tb=short -x
	@echo "=== [6/8] AUDIT ===" && $(PYTHON) tools/audit_engine.py --strict
	@echo "=== [7/8] CONTRACTS ===" && $(PYTHON) tools/verify_contracts.py
	@echo "=== [8/8] PAYLOAD COMPILER ===" && PYTHONPATH="$${PYTHONPATH}:." $(PYTHON) tools/payload_contract_compiler.py --stdout-only
	@echo ""
	@echo "╔══════════════════════════════════════════════╗"
	@echo "║  ALL 8 GATES PASSED ✓                         ║"
	@echo "╚══════════════════════════════════════════════╝"

agent-fix:  ## Auto-fix what can be fixed
	ruff check . --fix
	ruff format .

agent-full:  ## Full agent workflow: fix → check → coverage
	$(MAKE) agent-fix
	$(MAKE) agent-check
	$(PYTEST) tests/ -v --tb=short --cov=app --cov-report=term-missing

# ============================================================
# PUSH — agent-check then git push (no force)
# PUSH_SKIP_CHECK=1 make push  — skip agent-check
# ============================================================
push:  ## Run agent-check, then push current branch to origin
	@if [ "$(PUSH_SKIP_CHECK)" != "1" ]; then $(MAKE) agent-check; fi
	@if ! git diff-index --quiet HEAD -- 2>/dev/null; then \
		echo "WARN: uncommitted changes — only existing commits will be pushed"; \
	fi
	git push -u origin HEAD

# ============================================================
# PR — VALIDATE, THEN PUBLISH THROUGH THE GOVERNANCE CEREMONY
#   make pr PR_BASE=origin/main     # validate here, then publish/update ONE PR into PR_BASE
#   make pr-check [PR_BASE=...]      # validation only: agent-check + local_pr_pipeline guards
# PR_BASE is required and must resolve to a commit; there is no default base.
# Publication is delegated to the governance `make pr` (GOV, default
# ~/.cursor-governance): changed-file gate -> push -> open or update exactly one
# PR -> report its URL. It never merges; there is no raw `git push` here.
# Anything that fails before the hand-off publishes nothing.
# PYTHON: override to pin interpreter (default: .venv/bin/python if present, else python3)
# ============================================================
PR_PYTHON ?= $(shell if [ -x "$(CURDIR)/.venv/bin/python" ]; then printf '%s' "$(CURDIR)/.venv/bin/python"; else command -v python3; fi)
GOV ?= $(HOME)/.cursor-governance
PR_REMEDIATE ?= 0

pr: pr-base-guard pr-check  ## Validate, then publish/update one PR against an explicit PR_BASE (never merges)
	L9_EIE_PR_DELEGATED=1 PR_REMEDIATE="$(PR_REMEDIATE)" \
		make -C "$(GOV)" pr WS="$(CURDIR)" PR_BASE="$(PR_BASE)" PR_REMEDIATE="$(PR_REMEDIATE)"

pr-base-guard:  # runs before any validation or publication; fails closed
	@if [ -n "$$L9_EIE_PR_DELEGATED" ]; then \
		echo "pr: refusing re-entry from the governance ceremony; nothing published" >&2; exit 2; fi
	@if [ -z "$(PR_BASE)" ]; then \
		echo "pr: PR_BASE is required (e.g. make pr PR_BASE=origin/main); nothing published" >&2; exit 2; fi
	@if ! git rev-parse --verify --quiet "$(PR_BASE)^{commit}" >/dev/null; then \
		echo "pr: PR_BASE '$(PR_BASE)' does not resolve to a commit (fetch it first); nothing published" >&2; exit 2; fi
	@if [ ! -f "$(GOV)/Makefile" ]; then \
		echo "pr: governance ceremony not found at GOV=$(GOV); nothing published" >&2; exit 2; fi

pr-check: agent-check pr-guards  ## Local PR validation only. Never publishes.

pr-guards:  ## local_pr_pipeline guards: terminology, KB YAML, docs, links, contract-bound diff
	$(PR_PYTHON) local_pr_pipeline/check_compliance_terminology.py
	$(PR_PYTHON) local_pr_pipeline/compliance_kb_validate.py
	bash local_pr_pipeline/docs_consistency_local.sh
	$(PR_PYTHON) local_pr_pipeline/docs_link_check_local.py
	@if [ -n "$(PR_BASE)" ]; then \
		$(PR_PYTHON) local_pr_pipeline/contract_bound_local.py --base "$(PR_BASE)" --head HEAD; \
	else \
		echo "pr-guards: PR_BASE unset; contract-bound change gate skipped (set PR_BASE to run it)"; \
	fi

# ============================================================
# PR EVALUATE & MERGE — pr-pipeline.yml + agent review gate (gh CLI)
# merge: wait for CI on HEAD → agent comments resolved → required checks → merge
# Does NOT merge until CLEAN. Watches for new commits after you push fixes (default).
# Requires: gh auth login
# Usage:
#   make pr-evaluate              # pipeline + review gate, no merge
#   make pr-merge PR=42           # loop until CLEAN, then merge
#   make pr-ship PR=42            # alias
#   PR_MERGE_DRY_RUN=1 make pr-merge PR=42
#   PR_MERGE_WATCH=0 make pr-merge PR=42   # fail fast (no wait for fix push)
# Env: PR, PR_MERGE_WATCH, PR_MERGE_POLL_TIMEOUT, PR_SKIP_REVIEW_GATE, PR_AGENT_LOGINS
# ============================================================
pr-evaluate:  ## pr-pipeline + agent review gate; no merge
	bash scripts/pr_evaluate_and_merge.sh evaluate

pr-merge:  ## Until CLEAN: CI + review comments + required checks, then merge
	bash scripts/pr_evaluate_and_merge.sh merge $(PR)

pr-merge-dry:  ## Full gate, no merge (PR_MERGE_DRY_RUN=1)
	PR_MERGE_DRY_RUN=1 bash scripts/pr_evaluate_and_merge.sh merge $(PR)

pr-ship: pr-merge  ## Alias: evaluate until CLEAN then merge

pr-rerun:  ## Re-dispatch pr-pipeline.yml on PR head, then evaluate (no merge)
	PR_RERUN=1 bash scripts/pr_evaluate_and_merge.sh evaluate

# ============================================================
# BUILD / DEPLOY
# ============================================================
build:
	docker build -t $(IMAGE_NAME):latest .

# ============================================================
# DOCKER — LOCAL & PRODUCTION
# ============================================================
dev:
	docker compose up -d

dev-build:
	docker compose up -d --build

dev-down:
	docker compose down

dev-clean:
	docker compose down -v --remove-orphans

prod:
	docker compose -f $(COMPOSE_FILE) up -d

prod-build:
	docker compose -f $(COMPOSE_FILE) up -d --build

prod-down:
	docker compose -f $(COMPOSE_FILE) down

prod-logs:
	docker compose -f $(COMPOSE_FILE) logs -f $(SERVICE_NAME)

deploy:
	./scripts/deploy.sh $(ENV)

# ============================================================
# CLEANUP
# ============================================================
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name htmlcov -exec rm -rf {} + 2>/dev/null || true
	rm -rf dist/ build/ *.egg-info/
