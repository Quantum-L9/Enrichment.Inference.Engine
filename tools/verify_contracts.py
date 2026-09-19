#!/usr/bin/env python3
"""L9 Contract Verification Script (Enrichment.Inference.Engine).

Verifies active runtime/transport contract files declared in
`tools/l9_enrichment_manifest.yaml`.

Active contracts pin by `contract_version`, not by file digest, matching the
vocabulary `docs/contracts/node.constitution.yaml` already uses. A digest
re-stamps on every incidental edit -- a formatter pass or a lint fix rewrote
the file and the gate failed on a contract that had not changed -- so the pin
now tracks the contract, and the author bumps it when the contract moves.

The trade is explicit: this gate no longer detects silent content drift. It
verifies that every active contract is declared, versioned, present, and
referenced by its governance files; whether an edit changed the contract is a
review judgement, which is where that decision belongs.

Behavior:
- FAIL on missing active contract file
- FAIL on missing or malformed contract_version (MAJOR.MINOR.PATCH)
- FAIL on missing required reference file
- FAIL when an active contract path is not referenced by all declared governance files
- IGNORE deprecated_artifacts for pass/fail (informational only)
- PRESERVE KB YAML validation

Exit codes:
  0 = all checks pass
  1 = one or more FAIL conditions
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "tools" / "l9_enrichment_manifest.yaml"

ACTIVE_CONTRACT_GROUPS = (
    "active_transport_runtime",
    "engine_level",
)

# MAJOR.MINOR.PATCH, as docs/contracts/node.constitution.yaml declares it.
CONTRACT_VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+\Z")


def load_manifest() -> dict[str, Any]:
    """Load and validate the manifest file."""
    if yaml is None:
        print("FAIL: PyYAML not installed -- manifest verification cannot run")
        sys.exit(1)

    if not MANIFEST_PATH.exists():
        print(f"FAIL: Manifest not found at {MANIFEST_PATH}")
        sys.exit(1)

    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    if not isinstance(data, dict):
        print("FAIL: Manifest root must be a mapping")
        sys.exit(1)

    contracts = data.get("contracts")
    if not isinstance(contracts, dict):
        print("FAIL: Manifest missing top-level 'contracts' mapping")
        sys.exit(1)

    return data


def normalize_repo_path(path_str: str) -> str:
    """Normalize a repo-relative path string."""
    return path_str.lstrip("./")


def check_file_referenced(contract_path: str, ref_file: Path) -> bool:
    """Return True if the referenced governance file contains the contract path."""
    if not ref_file.exists():
        return False

    content = ref_file.read_text(encoding="utf-8")
    normalized = normalize_repo_path(contract_path)
    return normalized in content


def validate_contract_entry(
    entry: dict[str, Any],
    group_name: str,
) -> tuple[list[str], list[str]]:
    """Validate a single manifest contract entry."""
    fails: list[str] = []
    passes: list[str] = []

    path_value = entry.get("path")
    declared_version = entry.get("contract_version")
    required_refs = entry.get("required_refs", [])

    if not isinstance(path_value, str) or not path_value.strip():
        fails.append(f"FAIL: INVALID manifest entry in {group_name}: missing/invalid path")
        return fails, passes

    if not isinstance(declared_version, str) or not declared_version.strip():
        fails.append(f"FAIL: INVALID manifest entry for {path_value}: missing contract_version")
        return fails, passes

    if declared_version.startswith("<") and declared_version.endswith(">"):
        fails.append(f"FAIL: UNSTAMPED contract_version for active contract {path_value}")
        return fails, passes

    if not CONTRACT_VERSION_RE.match(declared_version):
        fails.append(
            f"FAIL: MALFORMED contract_version for {path_value}: "
            f"{declared_version!r} is not MAJOR.MINOR.PATCH"
        )
        return fails, passes

    if not isinstance(required_refs, list):
        fails.append(f"FAIL: INVALID required_refs for {path_value}: must be a list")
        return fails, passes

    full_path = REPO_ROOT / normalize_repo_path(path_value)
    if not full_path.exists():
        fails.append(f"FAIL: MISSING active contract file {path_value}")
        return fails, passes

    for ref in required_refs:
        if not isinstance(ref, str) or not ref.strip():
            fails.append(f"FAIL: INVALID required_ref for {path_value}: {ref!r}")
            continue

        ref_path = REPO_ROOT / normalize_repo_path(ref)
        if not ref_path.exists():
            fails.append(f"FAIL: REQUIRED reference file missing for {path_value}: {ref}")
            continue

        if not check_file_referenced(path_value, ref_path):
            fails.append(f"FAIL: {path_value} NOT referenced in required file {ref}")

    passes.append(f"PASS: {path_value}")
    return fails, passes


def check_critical_files() -> list[str]:
    """Verify critical structural files exist regardless of manifest."""
    critical = [
        "app/__init__.py",
        "app/main.py",
        "app/engines/__init__.py",
        "app/engines/handlers.py",
        "app/api/v1/chassis_endpoint.py",
        "app/services/chassis_handlers.py",
        "app/engines/orchestration_layer.py",
        "app/engines/graph_sync_client.py",
        "pyproject.toml",
        "requirements-ci.txt",
    ]
    fails: list[str] = []

    for rel_path in critical:
        if not (REPO_ROOT / rel_path).exists():
            fails.append(f"FAIL: MISSING critical file {rel_path}")

    return fails


def validate_deprecated_artifacts(manifest: dict[str, Any]) -> list[str]:
    """Return informational notes for deprecated artifacts."""
    notes: list[str] = []
    deprecated = manifest.get("deprecated_artifacts", [])

    if not deprecated:
        return notes

    if not isinstance(deprecated, list):
        notes.append("INFO: deprecated_artifacts section is invalid; expected a list")
        return notes

    for item in deprecated:
        if not isinstance(item, dict):
            notes.append(f"INFO: invalid deprecated_artifacts entry: {item!r}")
            continue

        path_value = item.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            notes.append("INFO: deprecated_artifacts entry missing valid path")
            continue

        rel_path = normalize_repo_path(path_value)
        exists = (REPO_ROOT / rel_path).exists()
        status = "present" if exists else "missing"
        notes.append(f"INFO: deprecated artifact {rel_path} is {status} (non-blocking)")

    return notes


def validate_kb_yaml() -> tuple[list[str], list[str]]:
    """Validate YAML files under kb/."""
    fails: list[str] = []
    notes: list[str] = []

    kb_dir = REPO_ROOT / "kb"
    if not kb_dir.exists():
        notes.append("INFO: kb/ directory not present")
        return fails, notes

    if yaml is None:
        fails.append("FAIL: PyYAML not installed -- cannot validate kb YAML files")
        return fails, notes

    for yaml_file in kb_dir.rglob("*.yaml"):
        try:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            if data is None:
                notes.append(f"INFO: Empty YAML file {yaml_file.relative_to(REPO_ROOT)}")
        except Exception as exc:  # noqa: BLE001
            fails.append(f"FAIL: Invalid YAML {yaml_file.relative_to(REPO_ROOT)}: {exc}")

    return fails, notes


def collect_active_contracts(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect all active contract entries from known groups."""
    contracts = manifest["contracts"]
    entries: list[dict[str, Any]] = []
    notes: list[str] = []

    for group_name in ACTIVE_CONTRACT_GROUPS:
        group_value = contracts.get(group_name, [])
        if not isinstance(group_value, list):
            notes.append(f"INFO: contracts.{group_name} missing or invalid; expected list")
            continue
        entries.extend(group_value)

    return entries, notes


def main() -> None:
    """Run verification and exit with pass/fail status."""
    manifest = load_manifest()

    fails: list[str] = []
    passes: list[str] = []
    notes: list[str] = []

    # Phase 1: Critical file checks
    fails.extend(check_critical_files())

    # Phase 2: Active contract checks
    active_contracts, active_notes = collect_active_contracts(manifest)
    notes.extend(active_notes)

    for group_name in ACTIVE_CONTRACT_GROUPS:
        group_entries = manifest["contracts"].get(group_name, [])
        if not isinstance(group_entries, list):
            continue

        for entry in group_entries:
            if not isinstance(entry, dict):
                fails.append(f"FAIL: INVALID manifest entry in {group_name}: {entry!r}")
                continue

            entry_fails, entry_passes = validate_contract_entry(entry, group_name)
            fails.extend(entry_fails)
            passes.extend(entry_passes)

    # Phase 3: Deprecated artifacts (informational only)
    notes.extend(validate_deprecated_artifacts(manifest))

    # Phase 4: KB YAML validation
    kb_fails, kb_notes = validate_kb_yaml()
    fails.extend(kb_fails)
    notes.extend(kb_notes)

    # Print results
    print("=" * 72)
    print("L9 Contract Verification Report")
    print("Engine: Enrichment.Inference.Engine")
    print("=" * 72)

    for item in passes:
        print(f"  {item}")
    for item in notes:
        print(f"  {item}")
    for item in fails:
        print(f"  {item}")

    print()
    print(f"Active contracts checked: {len(active_contracts)}")
    print(f"Passes:                 {len(passes)}")
    print(f"Failures:               {len(fails)}")

    if fails:
        print()
        print("RESULT: FAIL -- verification failed")
        sys.exit(1)

    print()
    print("RESULT: PASS -- all active contracts verified")
    sys.exit(0)


if __name__ == "__main__":
    main()
