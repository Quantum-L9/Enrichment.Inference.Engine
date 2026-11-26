"""PlasticOS cartridge 0.6.0 candidate: the Odoo facility-profile fields.

`domains/plasticos/candidates/0.6.0/spec.yaml` is a prepared, NOT published,
revision. It is not the authoring copy (`domains/plasticos/spec.yaml` stays the
published 0.5.0 bytes, see test_plasticos_publication.py) and it has no receipt:
activation is the repository owner's exact-byte review followed by
`scripts.domain_spec_operator publish` (docs/runbooks/native-domain-publication.md).

These tests pin what the candidate promises:

* every stored facility-profile data field of Odoo `plasticos.facility.profile`
  that 0.5.0 does not already declare under another name is a declared,
  evidence-only property of `Facility` and a field of the `facility` subject;
* every 0.5.0 declaration, the `material` subject, the roles, the node list,
  `ceg_owned` and the sync endpoints are unchanged;
* the CEG sync row built from 0.6.0 is identical to the one built from 0.5.0;
* each added field is accepted as a request-v2 target and observation for
  `subject.type = facility` by the resolver the converge handler uses, once the
  candidate is published (here: into an isolated store with a test-only
  receipt, which is not an approval of the candidate);
* `food_grade_certified` stays CEG-owned and is refused.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.engines.converge_request_v2 import (
    ConvergeRequestRefusedError,
    field_declarations,
    resolve_converge_request_v2,
)
from app.engines.domain_spec_compatibility import check_compatibility
from app.engines.domain_yaml_reader import DomainYamlReader, validate_native_spec
from app.engines.spec_types import row_properties, shape
from app.services.domain_spec_workflow import publish_effective_spec

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
TENANT = "plasticos"
TENANT_DIR = REPO / "domains" / TENANT
PUBLISHED_0_5_0 = "7e5787b059835a82f63b24592694b8eac289432133e2d07dcd9b50cfb91d5706"
# The reviewed candidate bytes (README). Before activation they live in
# candidates/0.6.0; activation installs them as an immutable revision and removes
# the candidate copy. Reading whichever exists keeps this suite valid across that
# step (Codex review on #348), and the pin proves it is the same bytes.
CANDIDATE_SHA = "590f0fdb6ce1181b4a716f3e00512bf5425ba42d266208aa4dab18afca8dafdb"
_CANDIDATE_FILE = TENANT_DIR / "candidates" / "0.6.0" / "spec.yaml"
CANDIDATE_PATH = (
    _CANDIDATE_FILE
    if _CANDIDATE_FILE.exists()
    else TENANT_DIR / "revisions" / CANDIDATE_SHA / "spec.yaml"
)
CANDIDATE_BYTES = CANDIDATE_PATH.read_bytes()
CANDIDATE: dict[str, Any] = yaml.safe_load(CANDIDATE_BYTES)
# The 0.5.0 baseline, from its immutable published revision (never the authoring
# copy, which becomes 0.6.0 on activation).
BASELINE: dict[str, Any] = yaml.safe_load(
    (TENANT_DIR / "revisions" / PUBLISHED_0_5_0 / "spec.yaml").read_bytes()
)
CEG: dict[str, Any] = yaml.safe_load((REPO / "tests/fixtures/ceg/plasticos.spec.yaml").read_bytes())

EQUIPMENT_CODES = [
    "horizontal_baler",
    "downstroke_baler",
    "shredder",
    "granulator",
    "wash_line",
    "pelletizer",
    "extruder",
    "compounder",
    "sorting_line",
    "bales",
    "regrind",
    "pellet",
    "flake",
    "rollstock",
    "metal_removal",
    "moisture_reduction",
    "fr_filtration",
    "fines_screening",
]
FEEDSTOCK = ["post_industrial", "post_consumer", "mixed", "virgin", "unknown"]
BOOLEANS = [
    "handles_bales",
    "handles_regrind",
    "handles_pellet",
    "handles_flake",
    "handles_rollstock",
    "can_remove_metal",
    "can_reduce_moisture",
    "can_filter_fr",
    "can_screen_fines",
    "iso_certified",
    "medical_grade_capable",
    "accepts_spot",
    "prefers_contract",
    "accepts_filled_materials",
]
# Field -> the unit Odoo stores, declared in the property's `unit` key.
NUMBERS = {
    "avg_truckload_lbs": "lb",
    "min_lot_size_lbs": "lb",
    "max_lot_size_lbs": "lb",
    "moisture_tolerance_pct": "percent",
    "pcr_pct_min": "percent",
    "pcr_pct_max": "percent",
}
# Operator decisions 2026-11-13: units on the 0.5.0 quantities. capacity is in
# metric tonnes; contamination_tolerance is a percent, as CEG declares it (0-100).
UNITS_0_5_0 = {
    "min_density": "g/cm3",
    "max_density": "g/cm3",
    "min_mfi": "g/10min",
    "max_mfi": "g/10min",
    "contamination_tolerance": "percent",
    "capacity_tons_month": "t/month",
}
STRINGS = ["form_preference_id", "process_type", "application_class"]
TEXT = ["freeform_notes", "application_notes"]
# In declaration order.
ADDED = [
    "equipment_type_ids",
    "form_preference_id",
    "process_type",
    "feedstock_type",
    "application_class",
    *BOOLEANS,
    *NUMBERS,
    *TEXT,
]
# Odoo field -> the 0.5.0 property it is aliased to by the Odoo consumer.
ALIASED = {
    "accepted_polymer_ids": "processed_polymers",
    "density_min": "min_density",
    "density_max": "max_density",
    "melt_index_min": "min_mfi",
    "melt_index_max": "max_mfi",
    "contamination_tolerance_pct": "contamination_tolerance",
    "capacity_lbs_month": "capacity_tons_month",
}
HELD_BACK = "food_grade_certified"

# One valid observed value per added field, by declared type.
SAMPLE: dict[str, object] = {
    "equipment_type_ids": ["granulator", "wash_line"],
    "form_preference_id": "regrind",
    "process_type": "mechanical",
    "feedstock_type": "post_consumer",
    "application_class": "packaging",
    **dict.fromkeys(BOOLEANS, True),
    **dict.fromkeys(NUMBERS, 40000.0),
    "freeform_notes": "Takes mixed bales on spot.",
    "application_notes": "Film-grade output.",
}


def _props(spec: dict[str, Any], label: str = "Facility") -> dict[str, dict[str, Any]]:
    node = next(n for n in spec["ontology"]["nodes"] if n["label"] == label)
    return {prop["name"]: prop for prop in node["properties"]}


def _subject(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in spec["subjects"]["types"] if t["name"] == name)


# ── Shape of the candidate ──────────────────────────────────────────────────


def test_candidate_is_a_valid_0_6_0_in_exactly_one_coherent_state() -> None:
    assert hashlib.sha256(CANDIDATE_BYTES).hexdigest() == CANDIDATE_SHA
    assert CANDIDATE["version"] == "0.6.0"
    assert validate_native_spec(CANDIDATE, TENANT) == CANDIDATE
    assert BASELINE["version"] == "0.5.0"
    pointer = json.loads((TENANT_DIR / "current.json").read_text(encoding="utf-8"))
    revision = TENANT_DIR / "revisions" / CANDIDATE_SHA
    if _CANDIDATE_FILE.exists():
        # Candidate: not published, not active. No revision, receipt or approval.
        assert pointer["sha256"] == PUBLISHED_0_5_0
        assert not revision.exists()
    else:
        # 0.6.0 stays the immutable activated candidate, chained on 0.5.0.
        # A later publication may move current.json; it does not rewrite 0.6.0.
        receipt = json.loads((revision / "receipt.json").read_text(encoding="utf-8"))
        assert receipt["previous_sha256"] == PUBLISHED_0_5_0
        assert receipt["version"] == "0.6.0"
        assert "test-fixture" not in receipt["reviewed_by"]
        assert (revision / "spec.yaml").read_bytes() == CANDIDATE_BYTES
        current = pointer["sha256"]
        if current == CANDIDATE_SHA:
            assert (TENANT_DIR / "spec.yaml").read_bytes() == CANDIDATE_BYTES
        else:
            successor = TENANT_DIR / "revisions" / current
            assert (successor / "spec.yaml").read_bytes() == (TENANT_DIR / "spec.yaml").read_bytes()
            # Current may be several publications past 0.6.0. The frozen
            # candidate stays on that chain; only its own receipt names 0.5.0.
            chain: list[str] = []
            sha: str | None = str(current)
            while isinstance(sha, str) and sha not in chain:
                chain.append(sha)
                hop = json.loads(
                    (TENANT_DIR / "revisions" / sha / "receipt.json").read_text(encoding="utf-8")
                )
                previous = hop["previous_sha256"]
                sha = previous if isinstance(previous, str) else None
            assert CANDIDATE_SHA in chain
            current_receipt = json.loads((successor / "receipt.json").read_text(encoding="utf-8"))
            assert current_receipt["version"] != "0.6.0"


def test_exactly_the_27_odoo_fields_are_added_all_evidence_only() -> None:
    assert len(ADDED) == len(set(ADDED)) == 27
    before, after = _props(BASELINE), _props(CANDIDATE)
    assert set(after) - set(before) == set(ADDED)
    assert all(after[name].get("evidenceonly") is True for name in ADDED)


def test_every_0_5_0_declaration_is_unchanged_but_for_its_declared_unit() -> None:
    before, after = _props(BASELINE), _props(CANDIDATE)
    assert set(before) <= set(after)
    for name, decl in before.items():
        unit = after[name].get("unit")
        assert unit == UNITS_0_5_0.get(name), name
        assert {k: v for k, v in after[name].items() if k != "unit"} == decl, name
    # 0.5.0 order is preserved; additions follow it.
    assert list(after)[: len(before)] == list(before)


def test_everything_outside_facility_properties_and_fields_is_unchanged() -> None:
    def outside(spec: dict[str, Any]) -> dict[str, Any]:
        # inference_rules and queryschema change only for the percent decision;
        # test_contamination_is_a_percent_everywhere pins exactly how.
        changed = {"version", "ontology", "subjects", "inference_rules", "queryschema"}
        rest = {k: v for k, v in spec.items() if k not in changed}
        nodes = [
            {k: v for k, v in n.items() if k != "properties"} for n in spec["ontology"]["nodes"]
        ]
        types = [
            {k: v for k, v in t.items() if not (t["name"] == "facility" and k == "fields")}
            for t in spec["subjects"]["types"]
        ]
        return {**rest, "nodes": nodes, "roles": spec["subjects"]["roles"], "types": types}

    assert outside(CANDIDATE) == outside(BASELINE)
    assert _subject(CANDIDATE, "material") == _subject(BASELINE, "material")


def test_added_fields_join_the_facility_subject_after_its_0_5_0_fields() -> None:
    before = _subject(BASELINE, "facility")["fields"]
    after = _subject(CANDIDATE, "facility")["fields"]
    assert after == [*before, *ADDED]


def test_every_property_belongs_to_exactly_one_subject() -> None:
    declared = set(_props(CANDIDATE)) - {"facility_id"}
    facility = set(_subject(CANDIDATE, "facility")["fields"])
    material = set(_subject(CANDIDATE, "material")["fields"])
    assert facility | material == declared
    assert facility & material == set()


def test_declared_types_and_units() -> None:
    props = _props(CANDIDATE)
    assert props["equipment_type_ids"]["type"] == "list"
    assert props["equipment_type_ids"]["items"] == {"type": "enum", "values": EQUIPMENT_CODES}
    assert props["feedstock_type"]["type"] == "enum"
    assert props["feedstock_type"]["values"] == FEEDSTOCK
    assert all(props[name]["type"] == "string" for name in [*STRINGS, *TEXT])
    # application_class is open: a string whose expected values are prose.
    for value in ("food", "medical", "automotive", "packaging", "agricultural", "construction"):
        assert value in props["application_class"]["description"]
    assert all(props[name]["type"] == "bool" for name in BOOLEANS)
    for name, unit in {**NUMBERS, **UNITS_0_5_0}.items():
        assert props[name]["type"] == "float"
        assert props[name]["unit"] == unit, name


def test_declared_units_are_served_as_the_v2_declaration(candidate_store: Path) -> None:
    """Units reach the consumer: response v2 section 10.2, field_evidence[*].declaration.unit."""
    reader = DomainYamlReader(candidate_store, require_published=True)
    resolved = resolve_converge_request_v2(
        TENANT, _request("0.6.0", targets=[{"field": name} for name in NUMBERS]), reader
    )
    assert resolved is not None
    declarations = field_declarations(resolved)
    for name, unit in {**NUMBERS, **UNITS_0_5_0}.items():
        assert declarations[name]["unit"] == unit, name
    assert "unit" not in declarations["name"]


def test_contamination_is_a_percent_everywhere() -> None:
    """0.5.0's grade rules tested a fraction (0.03, 0.07); CEG declares 0-100 percent."""
    rules = {rule["rule_id"]: rule for rule in CANDIDATE["inference_rules"]}
    old = {rule["rule_id"]: rule for rule in BASELINE["inference_rules"]}
    bounds = {
        rid: [
            (c["operator"], c["value"])
            for c in rules[rid]["conditions"]
            if c["field"] == "contamination_tolerance"
        ]
        for rid in ("grade-assignment-hdpe-a", "grade-assignment-hdpe-b")
    }
    assert bounds == {
        "grade-assignment-hdpe-a": [("LT", 3)],
        "grade-assignment-hdpe-b": [("GTE", 3), ("LTE", 7)],
    }
    for rid in bounds:
        assert rules[rid]["rule_or_model_version"] == "plasticos-inference-0.6.0"
    # Every other rule is untouched.
    assert {k: v for k, v in rules.items() if k not in bounds} == {
        k: v for k, v in old.items() if k not in bounds
    }
    query = {f["name"]: f for f in CANDIDATE["queryschema"]["fields"]}
    assert "percent" in query["contamination"]["description"]
    assert {k: v for k, v in CANDIDATE["queryschema"].items() if k != "fields"} == {
        k: v for k, v in BASELINE["queryschema"].items() if k != "fields"
    }


def test_aliased_odoo_fields_are_not_duplicated() -> None:
    props = _props(CANDIDATE)
    assert set(ALIASED).isdisjoint(props)
    assert set(ALIASED.values()) <= set(props)


def test_food_grade_certified_stays_ceg_owned_and_undeclared() -> None:
    """The frozen candidate's own ceg_owned lists stay as reviewed.

    Equality with the live CEG specimen is asserted only for the live cartridge
    in test_plasticos_subject_profiles.py. A later CEG property, such as
    entity_ref, must not fail this frozen candidate.
    """
    assert HELD_BACK not in _props(CANDIDATE)
    assert _subject(CANDIDATE, "material")["ceg_owned"] == [HELD_BACK]
    assert _subject(CANDIDATE, "facility")["ceg_owned"] == [
        "lat",
        "lon",
        "credit_score",
        "community_id",
        "rate_per_ton",
        "rate_confidence",
        "last_transaction_date",
    ]


# ── CEG sync row ────────────────────────────────────────────────────────────


def test_ceg_sync_row_is_identical_to_0_5_0() -> None:
    # The typed row contract (what packet_router checks and encodes, and what the
    # CEG compatibility check compares) is unchanged. A declared `unit` is not
    # part of it (spec_types.shape); it is the only difference allowed.
    after, before = row_properties(CANDIDATE, "Facility"), row_properties(BASELINE, "Facility")
    assert after is not None and before is not None
    assert {n: shape(d) for n, d in after.items()} == {n: shape(d) for n, d in before.items()}
    assert {n: {k: v for k, v in d.items() if k != "unit"} for n, d in after.items()} == before
    assert CANDIDATE["sync"] == BASELINE["sync"]


def test_candidate_passes_the_eie_ceg_sync_contract() -> None:
    current = yaml.safe_load((TENANT_DIR / "spec.yaml").read_bytes())
    result = check_compatibility(current, CEG)
    assert result["verdict"] == "pass", result["defects"]
    # 0.6.0 declared only code. The pinned specimen's map also names the line.
    historical = check_compatibility(CANDIDATE, CEG)
    assert historical["eie_version"] == "0.6.0"
    assert historical["verdict"] == "fail"
    assert any("line_id" in defect for defect in historical["defects"])


# ── Request v2 against the candidate once published ─────────────────────────


def _baseline_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real published 0.5.0 store, copied aside with 0.5.0 current.

    Built from the immutable revisions, never the live pointer, so it is the
    same before and after 0.6.0 is activated in the repository.
    """
    root = tmp_path_factory.mktemp("store")
    tenant = root / TENANT
    (tenant / "revisions").mkdir(parents=True)
    # Only the chain that ends at 0.5.0. Later publications (0.6.0, 0.6.1, ...)
    # stay out, so this isolated store can still publish the 0.6.0 candidate.
    sha: str | None = PUBLISHED_0_5_0
    seen: set[str] = set()
    while sha is not None and sha not in seen:
        seen.add(sha)
        revision = TENANT_DIR / "revisions" / sha
        shutil.copytree(revision, tenant / "revisions" / sha)
        receipt = json.loads((revision / "receipt.json").read_text(encoding="utf-8"))
        previous = receipt["previous_sha256"]
        sha = previous if isinstance(previous, str) else None
    pointer = {"schema": "eie.domain-current/v1", "sha256": PUBLISHED_0_5_0}
    (tenant / "current.json").write_text(json.dumps(pointer, separators=(",", ":")))
    return root


@pytest.fixture(scope="module")
def baseline_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _baseline_root(tmp_path_factory)


@pytest.fixture(scope="module")
def candidate_store(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real 0.5.0 store copied aside, then the candidate published onto it.

    The receipt written here is a test-only fixture so the published reader can
    load the candidate; it is not, and does not stand in for, owner approval.
    """
    root = _baseline_root(tmp_path_factory)
    receipt = publish_effective_spec(
        root,
        TENANT,
        CANDIDATE_BYTES,
        reviewed_input_sha256=CANDIDATE_SHA,
        reviewed_by="test-fixture (not a reviewer)",
        approval_ref="test-only isolated publication; not an approval",
        expected_previous_sha256=PUBLISHED_0_5_0,
    )
    assert receipt["previous_sha256"] == PUBLISHED_0_5_0
    assert receipt["version"] == "0.6.0"
    return root


def _request(version: str, **members: object) -> dict[str, Any]:
    return {
        "contract": "eie.converge.request/v2",
        "cartridge": {"id": TENANT, "version": version},
        "node": "Facility",
        "subject": {"type": "facility", "role": "supplier"},
        "entity": {"id": "plasticos.facility.profile:1", "name": "Acme Recycling"},
        **members,
    }


def _observation(name: str) -> dict[str, object]:
    observation: dict[str, object] = {
        "field": name,
        "value": SAMPLE[name],
        "source_kind": "odoo",
        "source_ref": "plasticos.facility.profile:1",
    }
    if name in NUMBERS:
        observation["unit"] = NUMBERS[name]
    return observation


@pytest.mark.parametrize("name", ADDED)
def test_each_added_field_is_a_target_and_an_observation(candidate_store: Path, name: str) -> None:
    reader = DomainYamlReader(candidate_store, require_published=True)
    declaration = {
        k: v for k, v in _props(CANDIDATE)[name].items() if k in {"type", "items", "values"}
    }
    resolved = resolve_converge_request_v2(
        TENANT,
        _request(
            "0.6.0",
            targets=[{"field": name, "type": declaration, "min_confidence": 0.8}],
            observations=[_observation(name)],
        ),
        reader,
    )
    assert resolved is not None
    assert resolved.config.source_sha256 == CANDIDATE_SHA
    assert resolved.subject == {"type": "facility", "role": "supplier"}
    # A list/map target carries its typed declaration (F04), and so does an enum,
    # whose allowed values a bare "enum" would lose (EIE-D-003); any other
    # scalar keeps its type name.
    declared = _props(CANDIDATE)[name]
    expected: object = declaration["type"]
    if declared["type"] in ("list", "map", "enum"):
        keys = ("type", "items", "properties", "values", "required")
        expected = {k: declared[k] for k in keys if k in declared}
    assert resolved.body["schema"] == {name: expected}
    assert [o["field"] for o in resolved.observations] == [name]


def test_all_added_fields_resolve_in_one_request(candidate_store: Path) -> None:
    reader = DomainYamlReader(candidate_store, require_published=True)
    resolved = resolve_converge_request_v2(
        TENANT,
        _request(
            "0.6.0",
            targets=[{"field": name} for name in ADDED],
            observations=[_observation(name) for name in ADDED],
        ),
        reader,
    )
    assert resolved is not None
    assert list(resolved.body["schema"]) == ADDED
    assert len(resolved.observations) == len(ADDED)


@pytest.mark.parametrize("member", ["targets", "observations"])
def test_food_grade_certified_is_refused(candidate_store: Path, member: str) -> None:
    reader = DomainYamlReader(candidate_store, require_published=True)
    members: dict[str, object] = {"targets": [{"field": "name"}]}
    if member == "targets":
        members["targets"] = [{"field": HELD_BACK}]
    else:
        members["observations"] = [
            {"field": HELD_BACK, "value": True, "source_kind": "odoo", "source_ref": "x"}
        ]
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2(TENANT, _request("0.6.0", **members), reader)
    assert refused.value.code == "target_not_declared"


# ── Version pins ────────────────────────────────────────────────────────────
# A tenant serves exactly one effective revision (current.json). A pin selects
# nothing; it is checked against that revision and refused on mismatch.


def test_a_0_5_0_pin_resolves_0_5_0_while_0_5_0_is_current(baseline_store: Path) -> None:
    reader = DomainYamlReader(baseline_store, require_published=True)
    request = _request("0.5.0", targets=[{"field": "name"}])
    request["cartridge"]["sha256"] = PUBLISHED_0_5_0
    resolved = resolve_converge_request_v2(TENANT, request, reader)
    assert resolved is not None
    assert resolved.config.version == "0.5.0"
    assert resolved.config.source_sha256 == PUBLISHED_0_5_0


def test_0_6_0_fields_are_not_targets_while_0_5_0_is_current(baseline_store: Path) -> None:
    reader = DomainYamlReader(baseline_store, require_published=True)
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2(
            TENANT, _request("0.5.0", targets=[{"field": "feedstock_type"}]), reader
        )
    assert refused.value.code == "target_not_declared"


def test_a_0_5_0_pin_is_refused_not_downgraded_once_0_6_0_is_current(
    candidate_store: Path,
) -> None:
    reader = DomainYamlReader(candidate_store, require_published=True)
    with pytest.raises(ConvergeRequestRefusedError) as refused:
        resolve_converge_request_v2(TENANT, _request("0.5.0", targets=[{"field": "name"}]), reader)
    assert refused.value.code == "cartridge_pin_mismatch"
