# GENERATED repository execution facade: make-v2. DO NOT EDIT.
#
# This file is owned by the central repository-execution tooling and is
# reproduced byte-for-byte into each consumer's root Makefile. It defines only
# the portable five-phase ABI and routes every phase to a repository-owned
# leaf in Repo.mk. Repository implementation, product logic, CI orchestration,
# and organization governance never live here.
#
# make-v2 adds test-fast: the bounded test selection the organization's
# required check runs on pull requests, merge groups and pushes. test stays
# the repository's whole suite.
#
# Regenerate from the versioned facade template with the repository-execution
# reconcile command; make-v2 behavior is immutable once released.
.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

# Composition, not isolation: the repository-owned implementation is included
# and must exist. A repository without Repo.mk has no execution implementation
# and fails here rather than passing silently.
include Repo.mk

.PHONY: help setup validate check test-fast test

help: ## Show the portable repository-execution phases
	@awk '\
		BEGIN { FS = ":.*## "; } \
		/^[A-Za-z0-9_.-]+:.*## / { \
			printf "  %-24s %s\n", $$1, $$2; \
		}' $(MAKEFILE_LIST)

# Portable make-v2 ABI. Each phase is exactly one repository-owned leaf.
setup: repo-setup ## Establish repository dependencies
validate: repo-validate ## Run repository validation
check: repo-check ## Run repository static and quality checks
test-fast: repo-test-fast ## Run the bounded pull-request test selection
test: repo-test ## Run repository tests
