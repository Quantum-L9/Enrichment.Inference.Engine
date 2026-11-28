"""Gitleaks configuration and install contracts (campaign finding T23).

Three regressions this file fails on:

1. A whole-directory or whole-extension path allowlist returns to
   `.gitleaks.toml`. Such an entry exempts every present and future file under
   it from secret scanning (l9.contract/validation-and-correctness@1:
   no `silent_skip_of_applicable_required_validation`). An exemption names one
   existing file, under the rule that fires, with a reason.
2. A global allowlist, or any allowlist regex that matches an ordinary word,
   returns. The former global `(?i)example|placeholder|dummy|test|fake|mock|sample`
   suppressed every finding whose match contained one of those substrings, a
   real credential included. Known false positives are exact fingerprints in
   `.gitleaksignore`.
3. A workflow installs the Gitleaks binary without verifying its SHA-256, or
   the workflows disagree on the pinned version. The pin is the one
   Quantum-L9/l9-ci-core selects.

The literals that look like credentials in this file are assembled at run time
so the file itself stays clean under the rules it tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

RELEASE_URL_MARK = "gitleaks/gitleaks/releases/download"

# The global path allowlist this repository carried until this contract.
LEGACY_BROAD_PATHS = (
    r"tests/",
    r"docs/",
    r"readme/",
    r"kb/",
    r"\.env\.example",
    r"\.env\.template",
    r"docker-compose\..*\.yml",
    r"requirements.*\.txt",
    r".*\.md$",
    r".*\.sarif$",
    r".*\.json$",
)


# ── Allowlist shape ───────────────────────────────────────────────────


def _config() -> dict:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))


def _as_list(value: object) -> list[dict]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _allowlists(config: dict) -> list[tuple[str, dict]]:
    """Every allowlist table in the config, global and per rule, in any spelling."""
    found: list[tuple[str, dict]] = []
    for key in ("allowlist", "allowlists"):
        found += [("global", table) for table in _as_list(config.get(key))]
    for rule in config.get("rules", []):
        for key in ("allowlist", "allowlists"):
            found += [(rule["id"], table) for table in _as_list(rule.get(key))]
    return found


def exact_file_or_reason(pattern: str) -> str | None:
    """Return None when `pattern` names exactly one file, else why it is broad."""
    if not (pattern.startswith("^") and pattern.endswith("$")):
        return "not anchored with ^ and $, so it matches any path containing it"
    literal = pattern[1:-1].replace(r"\.", "\x00")
    if re.search(r"[.*+?\[\]()|{}\\]", literal):
        return "contains a regex wildcard, class, group or alternation"
    if literal.endswith("/"):
        return "names a directory"
    return None


@pytest.mark.parametrize("pattern", LEGACY_BROAD_PATHS)
def test_every_legacy_broad_path_is_rejected(pattern: str) -> None:
    """Witness: each entry of the former global list fails the shape check."""
    assert exact_file_or_reason(pattern) is not None


@pytest.mark.parametrize(
    "pattern",
    [r"^tests/.*$", r"^docs/[^/]+\.md$", r"^(tests|docs)/a\.py$", r"^tests/$", r"^.*\.json$"],
)
def test_anchored_wildcards_are_still_rejected(pattern: str) -> None:
    assert exact_file_or_reason(pattern) is not None


def test_an_exact_file_pattern_is_accepted() -> None:
    assert exact_file_or_reason(r"^tests/unit/test_example\.py$") is None


def test_no_global_path_allowlist() -> None:
    """A path exemption is never global: it belongs to the rule that fires."""
    for owner, table in _allowlists(_config()):
        if owner == "global":
            assert not table.get("paths"), f"global allowlist exempts paths: {table.get('paths')}"


def test_every_path_exemption_names_one_existing_file_with_a_reason() -> None:
    for owner, table in _allowlists(_config()):
        paths = table.get("paths", [])
        if not paths:
            continue
        assert table.get("description", "").strip(), f"{owner}: path exemption without a reason"
        for pattern in paths:
            reason = exact_file_or_reason(pattern)
            assert reason is None, f"{owner}: {pattern!r} {reason}"
            named = pattern[1:-1].replace(r"\.", ".")
            assert (REPO_ROOT / named).is_file(), f"{owner}: {named} does not exist"


# The words the former global allowlist exempted. A credential may contain any
# of them, so no allowlist regex may match one.
COMMON_WORDS = ("example", "placeholder", "dummy", "test", "fake", "mock", "sample")

LEGACY_GLOBAL_REGEX = r"(?i)example|placeholder|dummy|test|fake|mock|sample"

IGNORE_FILE = REPO_ROOT / ".gitleaksignore"


def test_no_global_allowlist() -> None:
    """No global allowlist table: an exemption belongs to the rule that fires."""
    config = _config()
    assert not config.get("allowlist") and not config.get("allowlists"), "global allowlist present"


@pytest.mark.parametrize("word", COMMON_WORDS)
def test_no_allowlist_regex_matches_an_ordinary_word(word: str) -> None:
    for owner, table in _allowlists(_config()):
        for pattern in table.get("regexes", []) + table.get("stopwords", []):
            assert not re.search(pattern, word), (
                f"{owner}: {pattern!r} exempts any match containing {word!r}"
            )


def test_the_legacy_global_regex_is_rejected() -> None:
    """Witness for the check above: the former global regex fails it."""
    assert all(re.search(LEGACY_GLOBAL_REGEX, word) for word in COMMON_WORDS)


def _ignore_entries() -> list[str]:
    lines = IGNORE_FILE.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_every_ignore_entry_is_one_exact_fingerprint() -> None:
    """`.gitleaksignore` holds commit:path:rule:line fingerprints only, for known rules."""
    rule_ids = {rule["id"] for rule in _config().get("rules", [])}
    entries = _ignore_entries()
    assert entries, "the fingerprint list is empty; delete the file instead"
    for entry in entries:
        match = re.fullmatch(r"([0-9a-f]{40}):(.+):([a-z0-9-]+):(\d+)", entry)
        assert match, f"not a commit:path:rule:line fingerprint: {entry!r}"
        assert match.group(3) in rule_ids, f"unknown rule in {entry!r}"
    assert len(entries) == len(set(entries)), "duplicate fingerprint"


# ── Install steps ─────────────────────────────────────────────────────


def _install_steps() -> list[tuple[str, dict, dict]]:
    """(workflow, job, step) for every step that downloads a Gitleaks release."""
    steps = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps", []):
                if RELEASE_URL_MARK in (step.get("run") or ""):
                    steps.append((workflow.name, job, step))
    return steps


def _pin(job: dict, step: dict) -> dict[str, str]:
    env = {**(job.get("env") or {}), **(step.get("env") or {})}
    return {name: str(value) for name, value in env.items() if name.startswith("GITLEAKS_")}


def test_the_binary_installs_are_found() -> None:
    """Guard the discovery itself: an empty set would pass every check below."""
    names = {workflow for workflow, _, _ in _install_steps()}
    assert "gitleaks.yml" in names


# The org-CI cutover removed the repository-local `Security Scanning` job from
# both pipelines. It ran `gitleaks git .` over every fetched ref, so a branch's
# history (the cutover canary's planted fixture) failed unrelated pull requests.
# Core's `security / security` job in the required Analyze check runs gitleaks
# over the pull request's own range, plus pip-audit and Bandit; dependency
# review runs in supply-chain.yml.
_DUPLICATE_SCANS = ("gitleaks", "pip-audit", "bandit", "dependency-review-action")


@pytest.mark.parametrize(
    ("workflow", "gate"), [("ci.yml", "ci-gate"), ("pr-pipeline.yml", "pr-pipeline-gate")]
)
def test_the_legacy_security_scanning_job_stays_removed(workflow: str, gate: str) -> None:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    jobs = data["jobs"]
    assert "security" not in jobs, workflow
    assert all(job.get("name") != "Security Scanning" for job in jobs.values()), workflow
    for name, job in jobs.items():
        for step in job.get("steps", []):
            text = f"{step.get('run') or ''} {step.get('uses') or ''}"
            hits = [scan for scan in _DUPLICATE_SCANS if scan in text]
            assert not hits, f"{workflow}:{name} runs {hits}"
    assert "security" not in jobs[gate]["needs"], workflow


def test_dependency_review_still_runs_on_pull_requests() -> None:
    """The one Security Scanning step Core does not run keeps its own home."""
    data = yaml.safe_load((WORKFLOWS / "supply-chain.yml").read_text(encoding="utf-8"))
    on = data.get("on", data.get(True))
    assert "pull_request" in on
    # The removed pr-pipeline job reviewed pull requests to every target branch.
    # Its replacement must too: a branches / paths filter on the trigger silently
    # drops the review for PRs into any branch it does not name (EIE-PR-001).
    trigger = on["pull_request"] or {}
    narrowing = {"branches", "branches-ignore", "paths", "paths-ignore"} & set(trigger)
    assert not narrowing, f"supply-chain.yml pull_request is narrowed by {sorted(narrowing)}"
    job = data["jobs"]["dependency-review"]
    assert job.get("if") in (None, "github.event_name == 'pull_request'"), job.get("if")
    step = next(s for s in job["steps"] if "dependency-review-action" in (s.get("uses") or ""))
    assert step["with"]["fail-on-severity"] == "high"
    assert not step.get("continue-on-error")


@pytest.mark.parametrize("entry", _install_steps(), ids=lambda entry: entry[0])
def test_every_gitleaks_install_verifies_a_pinned_sha256(entry: tuple[str, dict, dict]) -> None:
    workflow, job, step = entry
    pin = _pin(job, step)
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin.get("GITLEAKS_VERSION", "")), workflow
    assert re.fullmatch(r"[0-9a-f]{64}", pin.get("GITLEAKS_SHA256", "")), workflow
    run = step["run"]
    assert "sha256sum" in run, f"{workflow}: download is not hashed"
    assert "GITLEAKS_SHA256" in run, f"{workflow}: hash is not compared with the pin"
    assert run.index("sha256sum") < run.index("tar "), f"{workflow}: extracted before verified"
    assert not step.get("continue-on-error"), workflow


def test_all_gitleaks_installs_use_one_pin() -> None:
    pins = {
        (workflow, tuple(sorted(_pin(job, step).items())))
        for workflow, job, step in _install_steps()
    }
    assert len({pin for _, pin in pins}) == 1, sorted(pins)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("workflow", ["gitleaks.yml"])
def test_install_step_refuses_a_download_with_the_wrong_checksum(
    workflow: str, tmp_path: Path
) -> None:
    """Witness: a tampered download is not extracted and not installed."""
    (job, step) = next((job, step) for name, job, step in _install_steps() if name == workflow)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = tmp_path / "installed"
    stubs = {
        "curl": 'while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done; echo tampered > "$out"',
        "tar": f"touch {installed}",
        "sudo": f"touch {installed}",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    assert "${{" not in step["run"]
    # A step with no `shell:` runs as `bash -e {0}` (workflow syntax reference).
    proc = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", **_pin(job, step)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "checksum mismatch" in proc.stdout + proc.stderr
    assert not installed.exists()


# ── The real binary against the repository's rules ────────────────────

_REAL_GITLEAKS = shutil.which("gitleaks")

_AWS_KEY_ID = "AKIA" + "Q7W3E9R5T1Y8U2I6"  # synthetic: key-ID shape, not a credential
_API_KEY_LINE = "api" + '_key = "' + "local-cache-slot-abcd" + '"\n'


def _scan(repo: Path, relative: str, content: str, config: Path = CONFIG) -> int:
    shutil.copy(config, repo / ".gitleaks.toml")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git_env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "fixture"]):
        subprocess.run(
            ["git", "-c", "user.name=witness", "-c", "user.email=witness@example.invalid", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            env=git_env,
        )
    assert _REAL_GITLEAKS is not None
    proc = subprocess.run(
        [_REAL_GITLEAKS, "git", ".", "--config", ".gitleaks.toml", "--no-banner", "--redact"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    return proc.returncode


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
@pytest.mark.parametrize(
    "relative",
    [
        "tests/unit/test_new_module.py",
        "docs/runbook.md",
        "readme/SETUP.md",
        "kb/plastics.yaml",
        ".env.example",
        ".env.template",
        "docker-compose.prod.yml",
        "requirements-dev.txt",
        "reports/scan.sarif",
        "config/settings.json",
    ],
)
def test_formerly_exempt_paths_are_scanned(relative: str, tmp_path: Path) -> None:
    """Witness: a key in each formerly exempt path class is now a finding."""
    assert _scan(tmp_path, relative, f"aws_access_key_id: {_AWS_KEY_ID}\n") == 1


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_the_named_fixture_exemption_covers_that_file_only(tmp_path: Path) -> None:
    named = tmp_path / "named"
    other = tmp_path / "other"
    named.mkdir()
    other.mkdir()
    assert _scan(named, "tests/unit/test_odoo_cross_repo_contract.py", _API_KEY_LINE) == 0
    assert _scan(other, "tests/unit/test_another_contract.py", _API_KEY_LINE) == 1


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_the_named_fixture_exemption_is_bound_to_its_rule(tmp_path: Path) -> None:
    """The exempt file is still scanned by every other rule."""
    relative = "tests/unit/test_odoo_cross_repo_contract.py"
    assert _scan(tmp_path, relative, f"aws_access_key_id: {_AWS_KEY_ID}\n") == 1


# Synthetic credentials that contain a formerly exempt word ("test"), assembled
# at run time. The former global allowlist suppressed both.
_AWS_KEY_ID_WITH_WORD = "AKIA" + "TEST" + "Q7W3E9R5T1Y8"
_OPENAI_KEY_WITH_WORD = "sk-" + "test" + "Zq8mK2vL9xR4tY7uW1e3"


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
@pytest.mark.parametrize(
    "content",
    [f"aws_access_key_id: {_AWS_KEY_ID_WITH_WORD}\n", f"OPENAI_API_KEY={_OPENAI_KEY_WITH_WORD}\n"],
    ids=["aws-key-id", "openai-key"],
)
def test_a_secret_containing_an_ordinary_word_is_a_finding(content: str, tmp_path: Path) -> None:
    """Witness: a credential whose text contains "test" is caught."""
    assert _scan(tmp_path, "app/settings.py", content) == 1


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_the_legacy_global_regex_would_have_hidden_it(tmp_path: Path) -> None:
    """Negative control: the same credential under the former global allowlist is silent."""
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(
        CONFIG.read_text(encoding="utf-8")
        + f'\n[allowlist]\nregexTarget = "match"\nregexes = ["""{LEGACY_GLOBAL_REGEX}"""]\n',
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    assert (
        _scan(
            repo, "app/settings.py", f"aws_access_key_id: {_AWS_KEY_ID_WITH_WORD}\n", config=legacy
        )
        == 0
    )


_DSN_PREFIX = "postgresql://" + "svc:" + "Zq8mK2vL9x" + "@"


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_connection_string_template_is_quiet_and_a_real_host_is_not(tmp_path: Path) -> None:
    """The `host:port` template is exempt; the same URL with a host is a finding."""
    template = tmp_path / "template"
    real = tmp_path / "real"
    template.mkdir()
    real.mkdir()
    assert _scan(template, "CONFIG.md", f"- Format: `{_DSN_PREFIX}host:port/dbname`\n") == 0
    assert _scan(real, "CONFIG.md", f"- Format: `{_DSN_PREFIX}db.internal.corp:5432/app`\n") == 1


# ── Org CI composition (Core security.yml) ─────────────────────────────
#
# Core scans with this file plus `[extend] useDefault = true`. A custom rule
# whose id equals a gitleaks default id is then replaced by the default rule, so
# a key this repository's own scan catches passed org CI (cutover canary #385:
# the repository gitleaks job failed on canary/eie_cutover/gitleaks_word_bad.py,
# the Core security job reported "no leaks found" on the same range).

# Default rule ids the custom rules used to reuse. None may come back.
SHADOWED_DEFAULT_IDS = ("openai-api-key", "generic-api-key", "slack-bot-token", "private-key")


def _core_composed(config_text: str) -> str:
    """The config Core security.yml builds from a repository `.gitleaks.toml`."""
    if tomllib.loads(config_text).get("extend") is None:
        config_text += "\n\n[extend]\nuseDefault = true\n"
    return config_text


def test_no_custom_rule_reuses_a_shadowed_default_id() -> None:
    rule_ids = {rule["id"] for rule in _config().get("rules", [])}
    assert not rule_ids & set(SHADOWED_DEFAULT_IDS), (
        "a custom rule reuses a gitleaks default id; org CI replaces it with the default"
    )


def _scan_rule_ids(tmp_path: Path, config_text: str, content: str) -> set[str]:
    config = tmp_path / "composed.toml"
    config.write_text(config_text, encoding="utf-8")
    target = tmp_path / "scan" / "app" / "settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(content, encoding="utf-8")
    report = tmp_path / "report.json"
    assert _REAL_GITLEAKS is not None
    subprocess.run(
        [
            _REAL_GITLEAKS,
            "dir",
            str(target.parent.parent),
            "--config",
            str(config),
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ],
        capture_output=True,
        check=False,
    )
    return {finding["RuleID"] for finding in json.loads(report.read_text(encoding="utf-8"))}


_OPENAI_LINE = f'OPENAI_API_KEY = "{_OPENAI_KEY_WITH_WORD}"\n'


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_the_custom_openai_rule_survives_org_ci_composition(tmp_path: Path) -> None:
    composed = _core_composed(CONFIG.read_text(encoding="utf-8"))
    assert "eie-openai-api-key" in _scan_rule_ids(tmp_path, composed, _OPENAI_LINE)


@pytest.mark.skipif(_REAL_GITLEAKS is None, reason="gitleaks binary not installed")
def test_a_shadowed_id_would_lose_the_finding_under_composition(tmp_path: Path) -> None:
    """Negative control: the former spelling, composed like Core, is silent."""
    text = CONFIG.read_text(encoding="utf-8")
    for rule_id in SHADOWED_DEFAULT_IDS:
        text = text.replace(f'id = "eie-{rule_id}"', f'id = "{rule_id}"')
    assert _scan_rule_ids(tmp_path, _core_composed(text), _OPENAI_LINE) == set()
