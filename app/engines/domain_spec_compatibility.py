"""Narrow EIE/CEG intersection checks; each native owner still validates its spec.

This module never compiles schemas, ranking, gates, or inference. A pass proves
only the declared sync intersection. Artifact approval and native admission are
separate release obligations.
"""

from __future__ import annotations

import re
from typing import Any

from app.engines.domain_yaml_reader import declared_spec_identity

RULE_SET = "eie-ceg-sync/v1"
_HASH = re.compile(r"[0-9a-f]{64}")


def _nodes(spec: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for node in spec.get("ontology", {}).get("nodes", []):
        result[node["label"]] = {
            prop["name"]: prop.get("type", "string") for prop in node.get("properties", [])
        }
    return result


def check_compatibility(
    eie_spec: dict[str, Any],
    ceg_spec: dict[str, Any],
    *,
    field_mappings: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Check validated native specs at outgoing sync references, never synonyms.

    Field mappings are operator-supplied assertions of meaning, checked against
    declared fields/types; this checker does not implement row transformation.
    Fields not declared by CEG are EIE-only evidence, not silently renamed into
    graph-owned fields. Endpoint id properties are injected by packet_router.
    """
    defects: list[str] = []
    eie_id, eie_version = declared_spec_identity(eie_spec)
    domain = ceg_spec.get("domain", {})
    ceg_id, ceg_version = domain.get("id"), domain.get("version")
    if not eie_id or eie_id != ceg_id:
        defects.append("tenant identity mismatch")
    eie_nodes, ceg_nodes = _nodes(eie_spec), _nodes(ceg_spec)
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
        owner = candidates[0]
        for field in ("method", "targetnode", "idproperty"):
            default = "POST" if field == "method" else None
            if endpoint.get(field, default) != owner.get(field, default):
                defects.append(f"{path}: {field} mismatch")
        target, identifier = owner.get("targetnode"), owner.get("idproperty")
        graph_fields = ceg_nodes.get(target)
        if graph_fields is None:
            defects.append(f"{path}: CEG target node is not declared")
            continue
        if not identifier or identifier not in graph_fields:
            defects.append(f"{path}: CEG id property is not declared on target node")
        evidence_fields = eie_nodes.get(target, {})
        for name in evidence_fields.keys() & graph_fields.keys():
            if evidence_fields[name] != graph_fields[name]:
                defects.append(f"{path}: shared field {target}.{name} type mismatch")
        checked += 1
    for label, mappings in (field_mappings or {}).items():
        if label not in eie_nodes or label not in ceg_nodes:
            defects.append(f"{label}: mapping label absent from one native ontology")
            continue
        for source, target in mappings.items():
            if source not in eie_nodes[label] or target not in ceg_nodes[label]:
                defects.append(f"{label}: mapping {source}->{target} references undeclared field")
            elif eie_nodes[label][source] != ceg_nodes[label][target]:
                defects.append(f"{label}: mapping {source}->{target} type mismatch")
    return {
        "rule_set": RULE_SET,
        "verdict": "fail" if defects else "pass",
        "tenant_id": eie_id,
        "eie_version": eie_version,
        "ceg_version": ceg_version,
        "endpoints_checked": checked,
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
