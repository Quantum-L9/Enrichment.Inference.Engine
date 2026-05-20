#!/usr/bin/env python3
"""Evaluate CI results using relevance-aware rules.

Relevant failures block.
Irrelevant failures become advisory.
Required jobs may never be skipped.
"""
from __future__ import annotations

import json
import os
import sys

REQUIRED_ALWAYS = ["validate", "security", "semgrep"]
OPTIONAL_JOBS = [
    "docker",
    "compliance",
    "typing",
    "audit",
    "test",
    "lint",
    "sbom",
    "scorecard",
]

# Explicit GitHub Actions conclusion handling.
PASS_RESULTS = {"success", "neutral"}
FAIL_RESULTS = {"failure", "cancelled", "timed_out", "action_required"}
SKIP_RESULTS = {"skipped"}


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "false").lower() == "true"


def _result(name: str) -> str:
    return os.environ.get(f"RESULT_{name.upper()}", "missing").lower()


def _is_failure(result: str) -> bool:
    return result in FAIL_RESULTS or result == "missing"


def main() -> int:
    relevance = {
        "lint": _env_bool("PYTHON_CHANGED") or _env_bool("APP_CHANGED"),
        "test": _env_bool("APP_CHANGED") or _env_bool("TESTS_CHANGED") or _env_bool("HAS_TESTING_LABEL"),
        "typing": _env_bool("TYPING_SENSITIVE_CHANGED") or _env_bool("HAS_TYPING_LABEL"),
        "audit": _env_bool("APP_CHANGED") or _env_bool("SECURITY_SENSITIVE_CHANGED"),
        "docker": _env_bool("DOCKER_CHANGED") or _env_bool("HAS_DOCKER_LABEL"),
        "compliance": _env_bool("SPEC_CHANGED") or _env_bool("ADR_CHANGED") or _env_bool("CONTRACTS_CHANGED"),
        "sbom": _env_bool("SBOM_RELEVANT") or _env_bool("DEPENDENCY_CHANGED"),
        "scorecard": _env_bool("SCORECARD_RELEVANT") or _env_bool("WORKFLOWS_CHANGED"),
    }

    failures: list[str] = []
    advisory: list[str] = []

    for job in REQUIRED_ALWAYS:
        result = _result(job)

        if result in SKIP_RESULTS:
            failures.append(f"required_skipped:{job}")
            continue

        if _is_failure(result):
            failures.append(f"required:{job}:{result}")

    for job in OPTIONAL_JOBS:
        result = _result(job)

        if relevance.get(job, False):
            if result in SKIP_RESULTS:
                advisory.append(f"relevant_skipped:{job}")
            elif _is_failure(result):
                failures.append(f"relevant:{job}:{result}")
        else:
            if _is_failure(result):
                advisory.append(f"irrelevant:{job}:{result}")

    summary = {
        "failures": failures,
        "advisory": advisory,
        "relevance": relevance,
    }

    print(json.dumps(summary, indent=2, sort_keys=True))

    if failures:
        print("ci_gate FAILED")
        return 1

    print("ci_gate PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
