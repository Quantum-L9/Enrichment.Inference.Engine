"""EIE declares Core's Repository Execution V2 contract (make-v1; issue #253).

"Analyze (central Core)" validates the contract, the byte-exact facade and the
Repo.mk ABI, then runs `make setup -> validate -> check -> test`. These checks
are the offline, repository-side half of that contract: they fail before CI
does when

* `.l9/repo-workflow.json` drifts from the exact three-field declaration,
* the root Makefile stops being the generated facade (it must only include
  Repo.mk and route the four phases to `repo-*` leaves),
* Repo.mk redefines a facade target, or loses a `.PHONY` leaf,
* a leaf's recipe references a script that does not exist, or reads an
  environment variable without a default (the facade runs bash -eu).

The byte-for-byte facade comparison itself belongs to Core
(`python3 -m tools.l9_repo --workspace <EIE> validate`); it is not copied here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / ".l9" / "repo-workflow.json"
FACADE = ROOT / "Makefile"
REPO_MK = ROOT / "Repo.mk"
PHASES = ("setup", "validate", "check", "test")
FACADE_TARGETS = ("help", *PHASES)
SCRIPT = re.compile(r"(?<![\w/.-])((?:[\w-]+/)+[\w.-]+\.(?:py|sh))\b")


def test_contract_is_the_exact_v2_declaration() -> None:
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == {
        "schema": "l9.repo-execution/v2",
        "facade": "make-v1",
        "required_phases": list(PHASES),
    }


def test_makefile_is_the_generated_facade() -> None:
    text = FACADE.read_text(encoding="utf-8")
    assert text.startswith("# GENERATED repository execution facade: make-v1. DO NOT EDIT.")
    assert "\ninclude Repo.mk\n" in text
    for phase in PHASES:
        assert re.search(rf"^{phase}: repo-{phase} ## ", text, re.MULTILINE), phase


def test_repo_mk_defines_the_four_leaves_and_no_facade_target() -> None:
    text = REPO_MK.read_text(encoding="utf-8")
    phony = " ".join(re.findall(r"^\.PHONY:(.*)$", text, re.MULTILINE)).split()
    for phase in PHASES:
        assert re.search(rf"^repo-{phase}:", text, re.MULTILINE), f"repo-{phase} missing"
        assert f"repo-{phase}" in phony, f"repo-{phase} not .PHONY"
    for target in FACADE_TARGETS:
        assert not re.search(rf"^{target}:", text, re.MULTILINE), f"Repo.mk redefines {target}"
        assert target not in phony, f"Repo.mk declares facade target {target} .PHONY"


def test_recipes_never_read_a_bare_environment_variable() -> None:
    # The facade sets SHELL flags -eu: an unset $$NAME aborts the recipe.
    recipes = "\n".join(
        line
        for line in REPO_MK.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    bare = re.findall(r"\$\$[A-Za-z_][A-Za-z0-9_]*", recipes)
    assert not bare, f"use $${{NAME:-}} under bash -eu: {sorted(set(bare))}"


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.parametrize("phase", PHASES)
def test_every_script_a_phase_runs_exists(phase: str) -> None:
    proc = subprocess.run(
        ["make", "-n", phase], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    scripts = {m for m in SCRIPT.findall(proc.stdout) if not m.startswith((".venv/", "/"))}
    missing = sorted(s for s in scripts if not (ROOT / s).is_file())
    assert missing == [], missing


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_test_phase_keeps_the_coverage_floor() -> None:
    proc = subprocess.run(
        ["make", "-n", "test"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "--cov-fail-under=71" in proc.stdout
    assert "tests/" in proc.stdout
