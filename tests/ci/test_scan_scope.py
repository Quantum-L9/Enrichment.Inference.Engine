"""Scanner scope: repository-owned code only.

The repository contract scanner and the anti-pattern scanners walk the file
system, so anything checked out or installed inside the workspace is in their
reach. ci.yml checks the CEG and Gate peers out under `.ci-subjects/`, each with
its own `.venv`; scanning them reported the Gate SDK's own `NodeRuntimeConfig`
call as an EIE callsite and failed the full catalog (deep run 44918690972,
`test_no_unproven_dynamic_calls`). These tests pin the scope so a peer
checkout or a nested virtualenv cannot be scanned as EIE code again, while
repository code at any depth still is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.ci._repository_contract_loader import load_catalog
from tests.ci._scan_utils import get_python_files, iter_python_files

if TYPE_CHECKING:
    import pathlib

OWNED = ("app/engines/handlers.py", "tools/audit_engine.py", "tests/unit/test_x.py")
FOREIGN = (
    ".ci-subjects/ceg-peer/engine/graph/driver.py",
    ".ci-subjects/gate-peer/.venv/lib/python3.12/site-packages/sdk/runtime/config.py",
    ".venv/lib/python3.12/site-packages/sdk/runtime/config.py",
    "tools/sidecar/.venv/lib/python3.12/site-packages/pkg/mod.py",
)


def _tree(root: pathlib.Path) -> None:
    for relative in OWNED + FOREIGN:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")


def _relative(root: pathlib.Path, paths: list[pathlib.Path]) -> set[str]:
    return {path.relative_to(root).as_posix() for path in paths}


def test_contract_scanner_scans_owned_code_and_no_peer_or_virtualenv(
    tmp_path: pathlib.Path,
) -> None:
    _tree(tmp_path)
    scan = load_catalog().scan
    found = _relative(tmp_path, iter_python_files(tmp_path, scan.include_globs, scan.exclude_globs))
    assert found == set(OWNED)


def test_anti_pattern_scanners_skip_peer_checkouts_and_virtualenvs(
    tmp_path: pathlib.Path,
) -> None:
    _tree(tmp_path)
    found = _relative(tmp_path, get_python_files(tmp_path))
    assert found == set(OWNED)


def test_anti_pattern_scanner_rooted_inside_a_peer_checkout_scans_that_root(
    tmp_path: pathlib.Path,
) -> None:
    """A skip name above the scan root does not empty the scan; one below it still applies."""
    root = tmp_path / ".ci-subjects" / "ceg-peer"
    for relative in ("engine/graph/driver.py", ".ci-subjects/nested/peer.py", ".venv/lib/x.py"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    assert _relative(root, get_python_files(root)) == {"engine/graph/driver.py"}


def test_anti_pattern_scanner_matches_whole_directory_names(tmp_path: pathlib.Path) -> None:
    """`build` and `.git` skip only directories with exactly those names."""
    for relative in (
        "app/services/prompt_builder.py",
        ".github/scripts/classify_pr.py",
        "build/lib/app/x.py",
        "pkg.egg-info/x.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n", encoding="utf-8")
    assert _relative(tmp_path, get_python_files(tmp_path)) == {
        "app/services/prompt_builder.py",
        ".github/scripts/classify_pr.py",
    }
