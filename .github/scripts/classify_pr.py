#!/usr/bin/env python3
"""Classify a GitHub Actions run by changed files and PR labels.

Primary signal: changed files.
Secondary signal: labels.
No broad whole-repo fallback is used because that creates false positives.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import PurePosixPath
from typing import Iterable

ZERO_SHA = "0000000000000000000000000000000000000000"


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT).strip()


def _event() -> dict[str, object]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}
    with open(event_path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    return loaded if isinstance(loaded, dict) else {}


def _nested_sha(container: object, *keys: str) -> str:
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "")


def _changed_files(event: dict[str, object]) -> tuple[list[str], bool]:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    if event_name == "pull_request":
        base_sha = _nested_sha(event, "pull_request", "base", "sha")
        head_sha = _nested_sha(event, "pull_request", "head", "sha")
        if base_sha and head_sha:
            try:
                _run(["git", "fetch", "--no-tags", "--depth=100", "origin", base_sha, head_sha])
                changed = _run(["git", "diff", "--name-only", f"{base_sha}...{head_sha}"])
                return changed.splitlines(), False
            except subprocess.CalledProcessError as exc:
                print(f"classifier warning: PR diff failed: {exc.output}", file=sys.stderr)
                return [], True

    before = str(event.get("before", ""))
    after = os.environ.get("GITHUB_SHA", "") or str(event.get("after", ""))
    if before and after and before != ZERO_SHA:
        try:
            changed = _run(["git", "diff", "--name-only", f"{before}...{after}"])
            return changed.splitlines(), False
        except subprocess.CalledProcessError as exc:
            print(f"classifier warning: push diff failed: {exc.output}", file=sys.stderr)
            return [], True

    # Unknown/new branch push. Do not classify the entire repo as changed.
    return [], True


def _labels(event: dict[str, object]) -> set[str]:
    raw = event.get("pull_request", {})
    if not isinstance(raw, dict):
        return set()
    labels_raw = raw.get("labels", [])
    labels: set[str] = set()
    if isinstance(labels_raw, list):
        for item in labels_raw:
            if isinstance(item, dict) and item.get("name"):
                labels.add(str(item["name"]).lower())
    return labels


def _match(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.strip("/")
    p = PurePosixPath(normalized)
    for pattern in patterns:
        clean = pattern.strip("/")
        if fnmatch.fnmatch(normalized, clean) or p.match(clean):
            return True
    return False


def _any(paths: list[str], patterns: list[str]) -> bool:
    return any(_match(path, patterns) for path in paths)


def _write_output(values: dict[str, object]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    lines: list[str] = []
    for key, value in values.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (list, dict)):
            rendered = json.dumps(value, separators=(",", ":"))
        else:
            rendered = str(value)
        lines.append(f"{key}={rendered}")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main() -> int:
    event = _event()
    files, diff_unknown = _changed_files(event)
    files = sorted(set(files))
    labels = _labels(event)

    python_changed = _any(files, ["**/*.py"])
    app_changed = _any(files, ["app/**/*.py"])
    tests_changed = _any(files, ["tests/**"])
    docs_changed = _any(files, ["README*", "docs/**", "*.md", "**/*.md"])
    workflows_changed = _any(files, [".github/workflows/**", ".github/dependabot.yml", ".github/actions/**"])
    scripts_changed = _any(files, [".github/scripts/**"])
    docker_changed = _any(files, ["Dockerfile", "Dockerfile.*", "docker/**", "docker-compose*.yml", ".dockerignore"])
    dependency_changed = _any(files, ["pyproject.toml", "requirements*.txt", "poetry.lock", "uv.lock", "Pipfile.lock"])
    spec_changed = _any(files, ["domains/**/spec.yaml", "spec.yaml", "**/*spec*.yaml"])
    adr_changed = _any(files, ["readme/adr/**", "docs/adr/**", "ADR/**"])
    contracts_changed = _any(files, ["config/contracts/**", "contracts/**", "tools/l9_template_manifest.yaml"])
    hooks_changed = _any(files, ["scripts/hooks/**", ".pre-commit-config.yaml"])

    security_sensitive_changed = workflows_changed or dependency_changed or docker_changed or _any(
        files,
        ["app/**/auth*.py", "app/**/security*.py", "app/**/gate*.py", "app/**/transport*.py"],
    )
    typing_sensitive_changed = app_changed or _any(
        files,
        ["**/models.py", "**/schemas.py", "**/transport*.py", "**/handlers.py", "requirements-ci.txt", "pyproject.toml"],
    )
    transport_sensitive_changed = _any(files, ["app/**/transport*.py", "app/**/packet*.py", "app/**/graph_return*.py"])
    ingress_sensitive_changed = _any(files, ["app/**/handlers.py", "app/**/chassis_handlers.py", "app/**/boot.py"])

    only_docs = bool(files) and docs_changed and not any(
        [python_changed, workflows_changed, scripts_changed, docker_changed, dependency_changed, spec_changed, contracts_changed]
    )
    only_types_dependency = dependency_changed and any("types-" in f.lower() for f in files) and not app_changed

    semgrep_relevant = app_changed or python_changed or security_sensitive_changed or "security" in labels
    sbom_relevant = dependency_changed or docker_changed or "dependencies" in labels or "security" in labels
    scorecard_relevant = workflows_changed or security_sensitive_changed or "security" in labels or "ci" in labels

    pr_class = "unknown"
    if diff_unknown:
        pr_class = "unknown_diff"
    elif workflows_changed or scripts_changed or "github-actions" in labels or "ci" in labels:
        pr_class = "ci_workflow"
    elif docker_changed or "docker" in labels:
        pr_class = "docker"
    elif only_docs:
        pr_class = "docs_only"
    elif tests_changed and not app_changed:
        pr_class = "tests_only"
    elif spec_changed or adr_changed or contracts_changed:
        pr_class = "compliance"
    elif security_sensitive_changed or "security" in labels:
        pr_class = "security"
    elif only_types_dependency or "typing" in labels:
        pr_class = "dependency_types"
    elif dependency_changed or "dependencies" in labels:
        pr_class = "dependency_python" if "python" in labels else "dependency"
    elif app_changed:
        pr_class = "app_code"

    outputs: dict[str, object] = {
        "all_changed_files": files,
        "changed_count": len(files),
        "diff_unknown": diff_unknown,
        "labels": sorted(labels),
        "pr_class": pr_class,
        "python_changed": python_changed,
        "app_changed": app_changed,
        "tests_changed": tests_changed,
        "docs_changed": docs_changed,
        "workflows_changed": workflows_changed,
        "scripts_changed": scripts_changed,
        "docker_changed": docker_changed,
        "dependency_changed": dependency_changed,
        "spec_changed": spec_changed,
        "adr_changed": adr_changed,
        "contracts_changed": contracts_changed,
        "hooks_changed": hooks_changed,
        "security_sensitive_changed": security_sensitive_changed,
        "typing_sensitive_changed": typing_sensitive_changed,
        "transport_sensitive_changed": transport_sensitive_changed,
        "ingress_sensitive_changed": ingress_sensitive_changed,
        "semgrep_relevant": semgrep_relevant,
        "sbom_relevant": sbom_relevant,
        "scorecard_relevant": scorecard_relevant,
        "only_types_dependency": only_types_dependency,
        "has_dependencies_label": "dependencies" in labels,
        "has_python_label": "python" in labels,
        "has_github_actions_label": "github-actions" in labels,
        "has_ci_label": "ci" in labels,
        "has_docker_label": "docker" in labels,
        "has_security_label": "security" in labels,
        "has_testing_label": "testing" in labels,
        "has_typing_label": "typing" in labels,
    }
    _write_output(outputs)
    print(json.dumps(outputs, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
