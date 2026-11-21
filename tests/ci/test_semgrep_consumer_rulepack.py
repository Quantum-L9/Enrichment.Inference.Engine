"""EIE consumer Semgrep rulepack declaration (l9-ci-sdk ADR 0021).

`.l9/semgrep-consumer-rules.yaml` makes the required `Analyze (central Core)`
check run `.semgrep/semgrep-rules.yaml` as a consumer execution. The SDK refuses
the whole consumer instance (which then blocks every PR) when a rule breaks its
admission contract. These checks fail the same mistakes locally, before a PR:
every listed file exists and holds only rules; every rule id is under the
consumer prefix, every rule carries its own non-empty `paths.include` and a
`metadata.l9.canonical_rule_id` in this repository's consumer namespace, and
no id or canonical id repeats. The SDK remains the authority; this mirrors the
subset of its contract that a rule edit in this repository can break.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DECLARATION = REPO_ROOT / ".l9" / "semgrep-consumer-rules.yaml"
OWNER = "Quantum-L9/Enrichment.Inference.Engine"
RULE_ID_PREFIX = "l9.eie."
CANONICAL_PREFIX = "CONSUMER-ENRICHMENT-INFERENCE-ENGINE-"


def _declaration() -> dict:
    return yaml.safe_load(DECLARATION.read_text(encoding="utf-8"))


def _rules() -> list[dict]:
    rules: list[dict] = []
    for relative in _declaration()["rule_files"]:
        document = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
        assert set(document) == {"rules"}, f"{relative}: a rule file holds only `rules`"
        rules.extend(document["rules"])
    return rules


def test_declaration_names_this_repository_and_existing_rule_files() -> None:
    declaration = _declaration()
    assert declaration["schema"] == "l9.semgrep-consumer-rulepack/v1"
    assert declaration["owner"] == OWNER
    assert declaration["rule_files"], "no rule files declared"
    for relative in declaration["rule_files"]:
        assert (REPO_ROOT / relative).is_file(), relative


def test_every_local_semgrep_rule_file_is_declared() -> None:
    """A rule file under .semgrep/ that is not declared would run locally only."""
    on_disk = {
        path.relative_to(REPO_ROOT).as_posix() for path in (REPO_ROOT / ".semgrep").glob("*.y*ml")
    }
    assert on_disk == set(_declaration()["rule_files"])


def test_every_rule_meets_the_consumer_admission_contract() -> None:
    rules = _rules()
    assert len(rules) == 8, "the eight EIE rules (Phase 1 row 23)"
    ids: list[str] = []
    canonical: list[str] = []
    for rule in rules:
        rule_id = rule["id"]
        assert rule_id.startswith(RULE_ID_PREFIX), rule_id
        include = (rule.get("paths") or {}).get("include")
        assert include, f"{rule_id}: paths.include must not be empty"
        canonical_id = rule["metadata"]["l9"]["canonical_rule_id"]
        assert canonical_id.startswith(CANONICAL_PREFIX), rule_id
        assert re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,99}", canonical_id), canonical_id
        ids.append(rule_id)
        canonical.append(canonical_id)
    assert len(set(ids)) == len(ids)
    assert len(set(canonical)) == len(canonical)


# ── no-sql-fstring witnesses (the rule matched nothing before this PR) ──

_SEMGREP = shutil.which("semgrep")

_SQL_CASES = {
    "bad_where": ("f\"SELECT * FROM users WHERE id = '{user_id}'\"", True),
    "bad_table": ('f"SELECT * FROM {table}"', True),
    "bad_delete": ('f"DELETE FROM x WHERE a = {value}"', True),
    "bad_update_triple": ("f'''UPDATE {table} SET a = {value}'''", True),
    "bad_insert": ('f"INSERT INTO {table} VALUES (1)"', True),
    "bad_upper_prefix": ('F"SELECT * FROM {table}"', True),
    "bad_raw_prefix_rf": ('rf"DELETE FROM x WHERE a = {value}"', True),
    "bad_raw_prefix_fr": ('fr"UPDATE {table} SET a = 1"', True),
    "bad_raw_prefix_upper": ('Rf"INSERT INTO {table} VALUES (1)"', True),
    "bad_lowercase": ('f"select * from {table}"', True),
    "bad_mixed_case": ('f"Delete from x where a = {value}"', True),
    "bad_lowercase_update": ('f"update {table} set a = 1"', True),
    "good_prose": ('f"Selected {count} items"', False),
    "good_no_interpolation": ('f"SELECT 1"', False),
    "good_parameterized": ('"SELECT * FROM t WHERE id = $1"', False),
    "good_lowercase_prose": ('f"selected rows: {count}"', False),
    "good_keyword_mid_text": ('f"Report: SELECT ran on {table}"', False),
    "good_nested_example_in_prose": ("f\"Example: f'SELECT {table}'\"", False),
}


@pytest.mark.skipif(_SEMGREP is None, reason="semgrep binary not installed")
def test_no_sql_fstring_fires_on_sql_fstrings_only(tmp_path: Path) -> None:
    lines = {}
    body = ["user_id = table = value = count = 1\n"]
    for name, (expression, _) in _SQL_CASES.items():
        lines[len(body) + 1] = name
        body.append(f"{name} = {expression}\n")
    target = tmp_path / "cases.py"
    target.write_text("".join(body), encoding="utf-8")
    rules = REPO_ROOT / ".semgrep" / "semgrep-rules.yaml"
    proc = subprocess.run(
        [
            _SEMGREP,
            "--config",
            str(rules),
            "--json",
            "--metrics=off",
            "--disable-version-check",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(proc.stdout)
    assert not report["errors"], report["errors"]
    fired = {
        lines[result["start"]["line"]]
        for result in report["results"]
        if result["check_id"].endswith("l9.eie.no-sql-fstring")
    }
    assert fired == {name for name, (_, bad) in _SQL_CASES.items() if bad}
