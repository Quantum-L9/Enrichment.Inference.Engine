"""EIE/CEG typed row-contract checks; each native owner still validates its spec.

This module never compiles schemas, ranking, gates, or inference. A pass proves
that every sync row EIE can send is exactly the row CEG declares for that
endpoint (rule set ``eie-ceg-sync/v2``). Artifact approval and native admission
are separate release obligations.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.engines.domain_yaml_reader import declared_spec_identity
from app.engines.spec_types import describe, node_properties, row_properties, shape

RULE_SET = "eie-ceg-sync/v2"
_HASH = re.compile(r"[0-9a-f]{64}")
_ENDPOINT_KEYS = ("method", "targetnode", "idproperty", "batchstrategy")
_CHILDSYNC_KEYS = ("field", "targetnode", "targetid", "edgetype", "edgedirection")
_TAXONOMY_KEYS = ("field", "edgetype", "targetlabel", "targetid")


def _entries(endpoint: Mapping[str, Any], key: str, fields: tuple[str, ...]) -> list[tuple]:
    raw = endpoint.get(key) or []
    if not isinstance(raw, list):
        return [("<malformed>",)]
    return sorted(
        tuple(str(entry.get(f)) for f in fields) if isinstance(entry, Mapping) else ("<malformed>",)
        for entry in raw
    )


def _shape_defects(path: str, eie: dict[str, Any], ceg: dict[str, Any]) -> list[str]:
    """Every difference between two typed shapes, named by its property path."""
    defects: list[str] = []
    for key in ("type", "required", "values"):
        if eie.get(key) != ceg.get(key):
            defects.append(f"{path}: {key} EIE {eie.get(key)!r} != CEG {ceg.get(key)!r}")
    if ("items" in eie) != ("items" in ceg):
        defects.append(f"{path}: items declared on one side only")
    elif "items" in eie:
        defects.extend(_shape_defects(f"{path}[]", eie["items"], ceg["items"]))
    eie_props, ceg_props = eie.get("properties"), ceg.get("properties")
    if (eie_props is None) != (ceg_props is None):
        defects.append(f"{path}: properties declared on one side only")
    elif eie_props is not None and ceg_props is not None:
        for name in sorted(eie_props.keys() - ceg_props.keys()):
            defects.append(f"{path}.{name}: declared by EIE, not by CEG")
        for name in sorted(ceg_props.keys() - eie_props.keys()):
            defects.append(f"{path}.{name}: declared by CEG, not by EIE")
        for name in sorted(eie_props.keys() & ceg_props.keys()):
            defects.extend(_shape_defects(f"{path}.{name}", eie_props[name], ceg_props[name]))
    return defects


def _row_contract(spec: Mapping[str, Any], label: str | None, *, eie: bool) -> dict | None:
    if not label:
        return None
    props = row_properties(spec, label) if eie else _all_properties(spec, label)
    if props is None:
        return None
    return {name: shape(decl) for name, decl in props.items()}


def _all_properties(spec: Mapping[str, Any], label: str) -> dict[str, dict[str, Any]] | None:
    props = node_properties(spec, label)
    return None if props is None else {p["name"]: p for p in props}


def _endpoint_defects(path: str, endpoint: dict, owner: dict, eie: dict, ceg: dict) -> list[str]:
    defects: list[str] = []
    for key in _ENDPOINT_KEYS:
        default = "POST" if key == "method" else None
        if endpoint.get(key, default) != owner.get(key, default):
            defects.append(
                f"{path}: {key} EIE {endpoint.get(key, default)!r} != "
                f"CEG {owner.get(key, default)!r}"
            )
    for key, fields in (("childsync", _CHILDSYNC_KEYS), ("taxonomyedges", _TAXONOMY_KEYS)):
        ours, theirs = _entries(endpoint, key, fields), _entries(owner, key, fields)
        if ours != theirs:
            defects.append(f"{path}: {key} EIE {ours} != CEG {theirs}")
    target = owner.get("targetnode")
    graph_row = _row_contract(ceg, target, eie=False)
    if graph_row is None:
        defects.append(f"{path}: CEG target node {target} is not declared")
        return defects
    identifier = owner.get("idproperty")
    if not identifier or identifier not in graph_row:
        defects.append(f"{path}: CEG id property is not declared on target node")
    evidence_row = _row_contract(eie, endpoint.get("targetnode"), eie=True)
    if evidence_row is None:
        defects.append(
            f"{path}: EIE declares no row for target node {endpoint.get('targetnode')} "
            "(declare the node's properties, or do not declare the endpoint)"
        )
        return defects
    defects.extend(
        _shape_defects(f"{path}: {target}", {"properties": evidence_row}, {"properties": graph_row})
    )
    return defects


def row_contract_summary(spec: Mapping[str, Any], label: str, *, eie: bool) -> dict[str, str]:
    """Human-readable typed row contract of one node (diagnostics and reports)."""
    props = row_properties(spec, label) if eie else _all_properties(spec, label)
    return {name: describe(decl) for name, decl in (props or {}).items()}


def check_compatibility(
    eie_spec: dict[str, Any],
    ceg_spec: dict[str, Any],
    *,
    field_mappings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare the FULL typed row contract of every sync endpoint EIE declares.

    For each EIE endpoint, CEG must declare exactly one endpoint at the same
    path with the same method, target node, id property, batch strategy,
    childsync (field, targetnode, targetid, edgetype, edgedirection) and
    taxonomyedges (field, edgetype, targetlabel, targetid); and the target
    node's row properties -- EIE's declarations that are not ``evidenceonly``
    against all of CEG's -- must match name for name and typed shape for
    typed shape (type spelling, required, enum values, list ``items``, map
    ``properties``, recursively). Any difference fails.

    There are no synonyms: ``field_mappings`` (a v1 notion) is refused when
    supplied, because no row is ever renamed between the engines. CEG
    endpoints EIE does not declare are listed in ``ceg_endpoints_not_produced``:
    EIE sends nothing to them, so they carry no EIE row contract.
    """
    defects: list[str] = []
    if field_mappings:
        defects.append(
            f"{RULE_SET} admits no field mappings: a sync row carries CEG's declared "
            "names, never a synonym"
        )
    eie_id, eie_version = declared_spec_identity(eie_spec)
    domain = ceg_spec.get("domain", {})
    ceg_id, ceg_version = domain.get("id"), domain.get("version")
    if not eie_id or eie_id != ceg_id:
        defects.append("tenant identity mismatch")
    outgoing = eie_spec.get("sync", {}).get("endpoints", [])
    incoming = ceg_spec.get("sync", {}).get("endpoints", [])
    if not outgoing:
        defects.append("EIE declares no outgoing sync endpoint")
    checked = 0
    for endpoint in outgoing:
        path = endpoint.get("path")
        candidates = [e for e in incoming if e.get("path") == path]
        if len(candidates) != 1:
            defects.append(f"{path}: expected exactly one CEG endpoint")
            continue
        defects.extend(_endpoint_defects(path, endpoint, candidates[0], eie_spec, ceg_spec))
        checked += 1
    produced = {e.get("path") for e in outgoing}
    return {
        "rule_set": RULE_SET,
        "verdict": "fail" if defects else "pass",
        "tenant_id": eie_id,
        "eie_version": eie_version,
        "ceg_version": ceg_version,
        "endpoints_checked": checked,
        "ceg_endpoints_not_produced": sorted(
            str(e.get("path")) for e in incoming if e.get("path") not in produced
        ),
        "defects": sorted(set(defects)),
        "field_mappings": field_mappings or {},
    }


def verify_pair_record(
    record: object,
    *,
    tenant_id: str,
    eie_sha256: str,
    ceg_sha256: str,
) -> bool:
    """Verify current bytes against a trusted pair record; no signature claim.

    Changing either artifact, including an independent rollback, requires a new
    compatible pair proof or restoration of an already verified exact pair.
    """
    if not isinstance(record, dict) or record.get("schema") != "eie.domain-compatible-pair/v1":
        return False
    if record.get("tenant_id") != tenant_id:
        return False
    proof = record.get("compatibility", {})
    if (
        not isinstance(proof, dict)
        or proof.get("verdict") != "pass"
        or proof.get("rule_set") != RULE_SET
        or not proof.get("proof_ref")
    ):
        return False
    for engine, digest in (("eie", eie_sha256), ("ceg", ceg_sha256)):
        artifact = record.get(engine)
        if (
            not isinstance(artifact, dict)
            or not _HASH.fullmatch(digest)
            or artifact.get("sha256") != digest
        ):
            return False
        if not artifact.get("spec_version") or not artifact.get("approval_ref"):
            return False
    return True
