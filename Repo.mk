# ============================================================
# Enrichment.Inference.Engine — repository Make implementation
# ============================================================
#
# REPOSITORY-OWNED. The root Makefile is the Core-owned make-v2 facade
# (Quantum-L9/l9-ci-core tools/l9_repo/facades/make-v2.mk), reproduced
# byte-for-byte and validated byte-for-byte by Core's repository verification
# (.l9/repo-workflow.json, Repository Execution V2). Never edit the Makefile:
# it owns help/setup/validate/check/test-fast/test and includes this file. Do
# not redefine any of those six targets here.
#
#   make setup     repo-setup     .venv + the full uv.lock profile (+ pre-commit hook)
#   make validate  repo-validate  contract manifest, payload contract compiler,
#                                 Gate_SDK channel (offline), audit engine --strict
#   make check     repo-check     ruff check, ruff format --check, strict mypy (type-check)
#   make test-fast repo-test-fast the bounded pull-request selection: a literal
#                                 list of test folders and files (no shell
#                                 expansion), so Core's PR test-expense judge
#                                 can read it.
#   make test      repo-test      the full pytest suite with the coverage floor
#                                 (C-15), locally and on scheduled/dispatched
#                                 runs. The full catalog, databases, peers, and
#                                 e2e are `make test-deep`, not this leaf.
#
# Facade make-v2 (.l9/repo-workflow.json). Core runs setup -> validate -> check
# -> test-fast on every pull request, on a plain runner (Python 3.12, no Docker,
# no services, no secrets); the PR-fast selection must pass there. `make test`
# is the whole suite and never runs on a pull request. `make test-deep`
# refuses a missing database, Redis, Neo4j, or pinned peer instead of skipping
# it.
#
# The facade runs every recipe under bash -eu -o pipefail: reference an
# environment variable as $${NAME:-}, never a bare $$NAME.
#
# The pre-V2 `make test` (quick, stop at the first failure) is `make test-quick`.
# Every other target below is unchanged.

.PHONY: repo-setup repo-validate repo-check repo-test-fast repo-test test-deep test-quick
.PHONY: dev dev-build dev-down dev-clean test-unit test-integration test-compliance test-ci test-contracts test-all test-watch lint lint-fix type-check audit audit-strict audit-json verify agent-check agent-fix agent-full build prod prod-build prod-down prod-logs deploy clean push pr pr-base-guard pr-check pr-guards pr-evaluate pr-merge pr-merge-dry pr-ship pr-rerun

IMAGE_NAME ?= enrichment-api
SERVICE_NAME ?= enrichment-api
COMPOSE_FILE ?= docker-compose.prod.yml
COVERAGE_MIN ?= 71
# Cobertura path for the PR-fast selection's coverage, read by the SonarCloud
# job in ci.yml. Empty (the default) measures nothing, so `make test` in
# Analyze (central Core) and locally is unchanged. When set, the same single
# PR-fast pytest run also records coverage of app/: no second test run.
# The C-15 floor (COVERAGE_MIN, .coveragerc fail_under) governs full-catalog
# runs only; the PR-fast selection was never floored (.coveragerc [report]
# comment), so this report passes --cov-fail-under=0 to keep that scope.
# Sonar's own new-code gate judges the changed lines this report covers.
PR_FAST_COVERAGE_XML ?=
PR_FAST_COVERAGE_ARGS = $(if $(PR_FAST_COVERAGE_XML),--cov=app --cov-report=xml:$(PR_FAST_COVERAGE_XML) --cov-fail-under=73)

PYTHON := $(shell if [ -x "$(CURDIR)/.venv/bin/python" ]; then printf '%s' "$(CURDIR)/.venv/bin/python"; else command -v python3; fi)
PYTEST := $(PYTHON) -m pytest

# ============================================================
# CORE REPOSITORY EXECUTION V2 PHASES (make-v1 ABI)
# ============================================================
repo-setup:  ## Create .venv if absent; install the full profile (project + every group) from uv.lock
	@test -x "$(CURDIR)/.venv/bin/python" || python3 -m venv "$(CURDIR)/.venv"
	UV_PROJECT_ENVIRONMENT="$(CURDIR)/.venv" PYTHON="$(CURDIR)/.venv/bin/python" bash tools/deps/install_locked.sh full
	"$(CURDIR)/.venv/bin/pre-commit" install

# The leaves below "Audit engine" were local-workflow checks only (ci.yml,
# pr-pipeline.yml, compliance.yml, l9-constitution-gate.yml,
# l9-contract-control.yml), none of them a required check. As `make validate`
# leaves they run in the required `Analyze (central Core)` check (EIE org-CI
# cutover, Phase 1 rows 11, 18, 20, 28, 31). The local copies stay until each
# leaf's negative witness is recorded (Phase 4).
repo-validate:  ## Contracts, payload compiler, Gate_SDK v1 (networked), audit --strict, KB rules, C-21 imports, constitution + attestation, workflow YAML (no mutation)
	@echo "── Contract manifest ──"
	$(PYTHON) tools/verify_contracts.py
	@echo "── Payload contract compiler ──"
	PYTHONPATH="$${PYTHONPATH:-}:." $(PYTHON) tools/payload_contract_compiler.py --stdout-only
	@echo "── Gate_SDK declared on the v1 channel (offline + networked tag) ──"
	$(PYTHON) scripts/validate_sdk_pin.py --verify-tag
	@echo "── Audit engine (fails on CRITICAL/HIGH) ──"
	$(PYTHON) tools/audit_engine.py --strict
	@echo "── KB rule schema ──"
	$(PYTHON) tools/validate_kb_rules.py
	@echo "── C-21: no deprecated chassis dispatch imports in app/ ──"
	$(PYTHON) local_pr_pipeline/check_compliance_terminology.py
	@echo "── Node constitution and runtime attestation ──"
	$(PYTHON) scripts/verify_node_constitution.py
	PYTHONPATH="$${PYTHONPATH:-}:." $(PYTHON) scripts/l9_contract_control.py verify-constitution
	PYTHONPATH="$${PYTHONPATH:-}:." $(PYTHON) scripts/l9_contract_control.py verify-attestation
	@echo "── Workflow YAML parses ──"
	$(PYTHON) tools/validate_workflow_yaml.py

# Service code may not print (Phase 1 row 29; the compliance.yml grep on app/ and
# engine/ was not a required check). A separate `--select T20` run over those
# directories only, so pyproject.toml's ruff select and its frozen ignore list
# (C-18 / INV-17) stay untouched and CLIs, tools, scripts and tests may print.
PRINT_BAN_PATHS := $(wildcard app engine)

repo-check:  ## Non-mutating static gate: ruff check, no print() in service code, ruff format --check, strict mypy
	@echo "── Ruff lint ──"
	$(PYTHON) -m ruff check .
	@echo "── No print() in service code (ruff T20: $(PRINT_BAN_PATHS)) ──"
	$(PYTHON) -m ruff check --select T20 --no-fix --ignore-noqa $(PRINT_BAN_PATHS)
	@echo "── Ruff format (check only) ──"
	$(PYTHON) -m ruff format --check .
	@echo "── Type check ──"
	$(MAKE) --no-print-directory type-check

# The pull-request selection (make-v2 `test-fast`). Every path is a literal, so
# Core's PR test-expense judge reads a bounded selection; a `$` expansion in
# the path list is judged UNKNOWN_SCRIPT. tests/unit and tests/contracts
# (tier2 included) hold every lane's own tests and the previous fixed PR-fast
# files, so a lane adding tests there edits nothing here. Paths outside them:
#   # base PR-fast contract selection
#     tests/services/test_gate_registration.py
#     tests/architecture/test_no_bespoke_gate_transport.py
#   # lane: eie-ceg-gate/eie-w
#     tests/test_pr21_packet_router.py
# tests/integration needs Neo4j, Redis, Postgres and pinned peers: it is not
# PR-safe and runs with the full suite (repo-test, test-deep), not here.
# Quarantine deselections must be literal too: the
# ledger is empty today, and a non-empty ledger fails here until its entries
# are written below as `--deselect <nodeid>`, never hidden behind an expansion.
# An empty or all-skipped selection fails.
repo-test-fast:  ## Bounded PR selection: tests/unit, tests/contracts and four named files
	@quarantined="$$($(PYTHON) tools/pytest_quarantine_deselects.py)"; \
	if [ -n "$${quarantined}" ]; then \
	  echo "PR-fast: the quarantine ledger is not empty; list each entry in repo-test-fast as a literal --deselect" >&2; \
	  exit 1; \
	fi; \
	report="$${TMPDIR:-/tmp}/eie-pr-fast-junit.xml"; \
	rm -f "$${report}"; \
	set +e; \
	TESTING=true PYTHONPATH="$${PYTHONPATH:-}:." PYTEST_ADDOPTS="$(PR_FAST_COVERAGE_ARGS)" $(PYTEST) \
	  tests/unit \
	  tests/contracts \
	  tests/services/test_gate_registration.py \
	  tests/architecture/test_no_bespoke_gate_transport.py \
	  tests/test_pr21_packet_router.py \
	  tests/ci/test_gitleaks_config_contract.py \
	  -p no:xdist \
	  --timeout=120 \
	  --tb=short -q \
	  --junitxml="$${report}"; \
	status=$$?; \
	set -e; \
	if [ "$${status}" -ne 0 ]; then exit "$${status}"; fi; \
	JUNIT_REPORT="$${report}" $(PYTHON) -c 'import os,sys,xml.etree.ElementTree as ET; root=ET.parse(os.environ["JUNIT_REPORT"]).getroot(); suites=list(root.iter("testsuite")) or [root]; tests=sum(int(s.get("tests") or 0) for s in suites); skipped=sum(int(s.get("skipped") or 0) for s in suites); failures=sum(int(s.get("failures") or 0) for s in suites); errors=sum(int(s.get("errors") or 0) for s in suites); passed=tests-skipped-failures-errors; print("PR-fast: tests=%s passed=%s skipped=%s" % (tests, passed, skipped)); sys.exit(0 if passed >= 1 else "PR-fast selection produced no passing test")'

# The whole suite with the C-15 floor (make-v2 `test`), locally and on
# scheduled or dispatched runs. It never runs on a pull request: pull requests
# run `test-fast`. The full catalog, databases, peers, and e2e are test-deep.
repo-test:  ## Full catalog with the C-15 coverage floor
	@deselects=(); while IFS= read -r line; do deselects+=("$${line}"); done < <($(PYTHON) tools/pytest_quarantine_deselects.py); \
	echo "Deselecting $${#deselects[@]} quarantined tests from the baseline ledger"; \
	TESTING=true PYTHONPATH="$${PYTHONPATH:-}:." $(PYTEST) tests/ -n auto "$${deselects[@]}" \
	  --cov=app --cov-report=term-missing --cov-fail-under=$(COVERAGE_MIN) \
	  --timeout=300 --tb=short -q

# One deep plan. Missing Redis, Neo4j, or a pinned CEG/Gate peer is NOT_RUN,
# which fails this leaf. The catalog ignores tests/integration so that
# collection runs once, under EIE_REQUIRE_INTEGRATION. tests/e2e stays out of
# this pytest; run_converge_e2e.sh fails closed when its rail or images are
# absent. Codecov is not uploaded from here.
test-deep:  ## Full catalog, integration, and e2e; a missing peer or database fails
	@$(PYTHON) -c 'exec("import os,socket,sys\nfrom urllib.parse import urlparse\ndef fail(msg):\n sys.exit(\"NOT_RUN: \"+msg)\ndef connect(name, default_port):\n raw=os.environ.get(name,\"\").strip() or fail(name+\" is not set\")\n parsed=urlparse(raw)\n host,port=parsed.hostname, parsed.port or default_port\n if not host or not port: fail(name+\" has no host\")\n s=socket.socket(); s.settimeout(5)\n try:\n  s.connect((host,port))\n except OSError as exc:\n  fail(\"%s is not accepting connections at %s:%s (%s)\"%(name,host,port,exc))\n finally:\n  s.close()\nconnect(\"REDIS_URL\",6379)\nconnect(\"NEO4J_TEST_URI\",7687)\nfor name in (\"L9_SEAM_CEG_ROOT\",\"L9_SEAM_GATE_ROOT\",\"CEG_REPO\",\"CEG_PYTHON\"):\n path=os.environ.get(name,\"\").strip()\n if not path or not os.path.exists(path): fail(\"pinned peer %s is missing\"%name)\n")'
	@deselects=(); while IFS= read -r line; do deselects+=("$${line}"); done < <($(PYTHON) tools/pytest_quarantine_deselects.py); \
	echo "Deselecting $${#deselects[@]} quarantined tests from the baseline ledger"; \
	TESTING=true PYTHONPATH="$${PYTHONPATH:-}:." $(PYTEST) tests/ \
	  --ignore=tests/integration --ignore=tests/e2e \
	  -n auto "$${deselects[@]}" \
	  --cov=app --cov-report=term-missing --cov-fail-under=$(COVERAGE_MIN) \
	  --timeout=300 --tb=short -q
	@TESTING=true EIE_REQUIRE_INTEGRATION=1 PYTHONPATH="$${PYTHONPATH:-}:." \
	  $(PYTEST) tests/integration -p no:xdist --timeout=300 --tb=short -q
	bash tests/e2e/run_converge_e2e.sh

# ============================================================
# TESTING — TIERED
# ============================================================
test-quick:  ## Quick: stop at the first failing test (the pre-V2 `make test`)
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
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(MAKE) type-check
	$(PYTEST) tests/ -v --tb=short --cov=app --cov-report=term-missing --cov-fail-under=$(COVERAGE_MIN)

test-watch:  ## Watch mode for unit tests
	$(PYTEST) tests/unit/ -- -v --tb=short -w

# ============================================================
# QUALITY — LINT + TYPE CHECK
# ============================================================
lint:  ## Lint + format check + type check
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(MAKE) type-check

lint-fix:  ## Auto-fix lint and format issues
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format .

type-check:  ## Run strict MyPy; any finding blocks.
	$(PYTHON) -m mypy app --show-error-codes
	$(PYTHON) tools/mypy_ratchet.py

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
	@echo "=== [1/8] LINT ===" && $(PYTHON) -m ruff check .
	@echo "=== [2/8] FORMAT ===" && $(PYTHON) -m ruff format --check .
	@echo "=== [3/8] TYPES (strict MyPy) ===" && $(MAKE) type-check
	@echo "=== [4/8] UNIT TESTS ===" && $(PYTEST) tests/unit/ tests/compliance/ -v --tb=short -x
	@echo "=== [5/8] CI TESTS ===" && $(PYTEST) tests/ci/ -v --tb=short -x
	@echo "=== [6/8] AUDIT ===" && $(PYTHON) tools/audit_engine.py --strict
	@echo "=== [7/8] CONTRACTS ===" && $(PYTHON) tools/verify_contracts.py
	@echo "=== [8/8] PAYLOAD COMPILER ===" && PYTHONPATH="$${PYTHONPATH:-}:." $(PYTHON) tools/payload_contract_compiler.py --stdout-only
	@echo ""
	@echo "╔══════════════════════════════════════════════╗"
	@echo "║  ALL 8 GATES PASSED ✓                         ║"
	@echo "╚══════════════════════════════════════════════╝"

agent-fix:  ## Auto-fix what can be fixed
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format .

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
	@if [ -n "$${L9_EIE_PR_DELEGATED:-}" ]; then \
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
