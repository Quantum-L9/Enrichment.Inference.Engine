#!/usr/bin/env python3
"""Evaluate CI results using relevance-aware rules.

Fail only on required relevant jobs.
Skipped or advisory jobs do not block merges.
"""
from __future__ import annotations

import json
import os
import sys

REQUIRED_ALWAYS = ["validate", "security"]
OPTIONAL_JOBS = ["docker", "compliance", "typing", "audit", "test", "lint"]


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "false").lower() == "true"


def _result(name: str) -> str:
    return os.environ.get(f"RESULT_{name.upper()}", "skipped").lower()


def main() -> int:
    relevance = {
        "lint": _env_bool("PYTHON_CHANGED") or _env_bool("APP_CHANGED"),
        "test": _env_bool("APP_CHANGED") or _env_bool("TESTS_CHANGED") or _env_bool("HAS_TESTING_LABEL"),
        "typing": _env_bool("TYPING_SENSITIVE_CHANGED") or _env_bool("HAS_TYPING_LABEL"),
        "audit": _env_bool("APP_CHANGED") or _env_bool("SECURITY_SENSITIVE_CHANGED"),
        "docker": _env_bool("DOCKER_CHANGED") or _env_bool("HAS_DOCKER_LABEL"),
        "compliance": _env_bool("SPEC_CHANGED") or _env_bool("ADR_CHANGED") or _env_bool("CONTRACTS_CHANGED"),
    }

    failures: list[str] = []
    advisory: list[str] = []

    for job in REQUIRED_ALWAYS:
        result = _result(job)
        if result not in {"success", "skipped"}:
            failures.append(f"required:{job}:{result}")

    for job in OPTIONAL_JOBS:
        result = _result(job)
        if relevance.get(job, False):
            if result not in {"success", "skipped"}:
                failures.append(f"relevant:{job}:{result}")
        else:
            if result == "failure":
                advisory.append(f"irrelevant:{job}:{result}")

    summary = {
        "failures": failures,
        "advisory": advisory,
        "relevance": relevance,
    }

    print(json.dumps(summary, indent=2, sort_keys=True))

    if failures:
        print("❌ ci_gate FAILED")
        return 1

    print("✅ ci_gate PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
