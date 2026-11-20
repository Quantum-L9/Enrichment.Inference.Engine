# --- L9_META ---
# l9_schema: 1
# origin: native
# engine: enrichment
# layer: [ci]
# tags: [ci, fail-open, regression]
# owner: platform
# status: active
# --- /L9_META ---
"""Workflow steps that reported success without being able to fail.

- docs-sync: BROKEN was incremented inside a piped `while` loop (a subshell),
  so the step could never fail. The step's own shell runs here against a tree
  with a broken link and must exit non-zero.
- docs-consistency: four steps SKIPped with exit 0 because their target files
  do not exist. Every remaining step names a file this repository has, and a
  missing required file FAILs.
- pr-review-enforcement: the PR-size check announced "blocked from merging" but
  never failed. It is housekeeping, so it now says advisory and never fails.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _steps(workflow: str) -> list[dict]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    return [step for job in data["jobs"].values() for step in job["steps"]]


def _step(workflow: str, name: str) -> dict:
    return next(step for step in _steps(workflow) if step.get("name") == name)


def _bash(script: str, cwd: Path, step: dict | None = None) -> subprocess.CompletedProcess[str]:
    # The shell GitHub runs on ubuntu: `bash -e {0}` for a `run:` with no
    # `shell:`, and `bash --noprofile --norc -eo pipefail {0}` for `shell: bash`.
    explicit = (step or {}).get("shell") == "bash"
    argv = ["bash", "--noprofile", "--norc", "-eo", "pipefail"] if explicit else ["bash", "-e"]
    return subprocess.run(
        [*argv, "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _docs_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


DOCS_LINKS = "Check Markdown Links"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_sync_fails_on_a_broken_internal_link(tmp_path: Path) -> None:
    step = _step("docs-sync.yml", DOCS_LINKS)
    tree = _docs_tree(tmp_path, {"docs/a.md": "see [missing](./nope.md)\n"})
    result = _bash(step["run"], tree, step)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Broken link in docs/a.md: ./nope.md" in result.stdout
    assert "Found 1 broken internal link(s)" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_sync_passes_valid_links_including_anchors_and_titles(tmp_path: Path) -> None:
    step = _step("docs-sync.yml", DOCS_LINKS)
    tree = _docs_tree(
        tmp_path,
        {
            "docs/a.md": '[b](./b.md) [sec](./b.md#part) [t](./b.md "Title") [up](../README.md)\n',
            "docs/b.md": "# part\n",
            "README.md": "root\n",
            "readme/c.md": "[web](https://example.com) [abs](/x.md)\n",
        },
    )
    result = _bash(step["run"], tree, step)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All documentation links are valid" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_sync_passes_on_this_repository() -> None:
    step = _step("docs-sync.yml", DOCS_LINKS)
    result = _bash(step["run"], ROOT, step)
    assert result.returncode == 0, result.stdout + result.stderr


RETIRED_TARGETS = ("AGENT.md", "GUARDRAILS.md", "AGENT_BOOTSTRAP.md")


def test_docs_consistency_names_only_files_this_repository_has() -> None:
    text = (WORKFLOWS / "docs-consistency.yml").read_text(encoding="utf-8")
    runs = "\n".join(step.get("run", "") for step in _steps("docs-consistency.yml"))
    for retired in RETIRED_TARGETS:
        assert f'"{retired}"' not in runs and f" {retired}" not in runs, retired
    assert '"INVARIANTS.md"' not in runs
    for required in ("AGENTS.md", "docs/INVARIANTS.md", "docs/CI_WHITELIST_REGISTER.md"):
        assert required in runs and (ROOT / required).is_file(), required
    assert (ROOT / "docs/adr").is_dir()
    # The header records what was retargeted and retired.
    assert "retired" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_consistency_required_steps_run_and_pass_without_skipping() -> None:
    for step in _steps("docs-consistency.yml"):
        script = step.get("run")
        if not script or "git diff" in script or "app/api" in script:
            continue  # WARN-only steps that read history or route counts
        result = _bash(script, ROOT, step)
        assert result.returncode == 0, (step["name"], result.stdout + result.stderr)
        assert "SKIP" not in result.stdout, (step["name"], result.stdout)
        assert "PASS" in result.stdout, (step["name"], result.stdout)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_consistency_missing_required_file_fails(tmp_path: Path) -> None:
    step = _step("docs-consistency.yml", "Verify docs/INVARIANTS.md contains INV-1 through INV-20")
    result = _bash(step["run"], tmp_path, step)
    assert result.returncode == 1
    assert "FAIL: docs/INVARIANTS.md not found" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_docs_consistency_inv_1_is_not_satisfied_by_inv_10(tmp_path: Path) -> None:
    step = _step("docs-consistency.yml", "Verify docs/INVARIANTS.md contains INV-1 through INV-20")
    body = "\n".join(f"INV-{i}" for i in range(10, 21))  # no INV-1..INV-9
    _docs_tree(tmp_path, {"docs/INVARIANTS.md": body + "\n"})
    result = _bash(step["run"], tmp_path, step)
    assert result.returncode == 1
    assert "missing INV-1" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("decoy", ["XINV-{i}", "INV-{i}x", "INV_{i}", "INV-{i}0"])
def test_docs_consistency_inv_needs_identifier_boundaries(tmp_path: Path, decoy: str) -> None:
    step = _step("docs-consistency.yml", "Verify docs/INVARIANTS.md contains INV-1 through INV-20")
    lines = [f"| INV-{i} |" for i in range(2, 21)] + [decoy.format(i=1)]  # INV-1 only as decoy
    _docs_tree(tmp_path, {"docs/INVARIANTS.md": "\n".join(lines) + "\n"})
    result = _bash(step["run"], tmp_path, step)
    assert result.returncode == 1, result.stdout
    assert "missing INV-1" in result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("form", ["INV-{i}", "### INV-{i}: title", "| INV-{i} |", "**INV-{i}**"])
def test_docs_consistency_inv_accepts_standalone_and_headings(tmp_path: Path, form: str) -> None:
    step = _step("docs-consistency.yml", "Verify docs/INVARIANTS.md contains INV-1 through INV-20")
    body = "\n".join(form.format(i=i) for i in range(1, 21))
    _docs_tree(tmp_path, {"docs/INVARIANTS.md": body + "\n"})
    result = _bash(step["run"], tmp_path, step)
    assert result.returncode == 0, result.stdout


def test_pr_size_check_is_advisory_and_says_so() -> None:
    script = _step("pr-review-enforcement.yml", "Check PR Size")["with"]["script"]
    assert "blocked from merging" not in script
    assert "BLOCKED" not in script
    assert "setFailed" not in script
    assert "never blocks merging" in script
    assert "Over limit (advisory)" in script


_GITHUB_SCRIPT_HARNESS = """
const failing = process.argv.slice(1).includes("fail");
const warnings = [];
const reject = async () => { throw new Error("Resource not accessible by integration"); };
const ok = async () => ({ data: [] });
const github = { rest: { issues: {
  listComments: failing ? reject : ok, createComment: failing ? reject : ok,
  updateComment: failing ? reject : ok } } };
const core = { warning: (m) => warnings.push(m),
               setFailed: (m) => { throw new Error("setFailed: " + m); } };
const context = { repo: { owner: "o", repo: "r" },
  payload: { pull_request: { number: 1, additions: 5000, deletions: 0, changed_files: 3 } } };
const script = require("fs").readFileSync(0, "utf8");
new (Object.getPrototypeOf(async function () {}).constructor)("github", "context", "core", script)(
  github, context, core
).then(() => console.log(JSON.stringify(warnings)), (e) => { console.error(e.message); process.exit(1); });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("api", ["ok", "fail"])
def test_pr_size_check_never_fails_even_when_comment_api_rejects(api: str) -> None:
    """Fork PRs get a read-only token; a comment API error must not fail the check."""
    script = _step("pr-review-enforcement.yml", "Check PR Size")["with"]["script"]
    script = (
        script.replace("${{ env.PR_WARN_LINES }}", "300")
        .replace("${{ env.PR_BLOCK_LINES }}", "1000")
        .replace("${{ env.PR_MAX_FILES }}", "50")
    )
    result = subprocess.run(
        ["node", "-e", _GITHUB_SCRIPT_HARNESS, "--", api],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    warnings = result.stdout
    assert "Advisory: PR exceeds size limits" in warnings
    assert ("comment not published" in warnings) is (api == "fail")
