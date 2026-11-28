"""
--- L9_META ---
l9_schema: 1
origin: engine-specific
engine: enrichment
layer: [engines]
tags: [convergence, contract, converge-request-v2]
owner: engine-team
status: active
--- /L9_META ---

Converge request v2 (docs/contracts/converge/request.v2.md, frozen 2026-11-13):
explicit tenant, cartridge, node, subject and typed field targets.

A v2 body is resolved completely against the cartridge it names before
anything runs. It either becomes a v1-shaped body the canonical converge path
already understands (`domain_id`, `node_label`, `schema`, `field_thresholds`)
together with the one cartridge revision it was checked against, or it is
refused whole with a contract code. Nothing here falls back, defaults or
branches on a particular cartridge, node, subject or field name.

Request v2.1 (additive, frozen 2026-11-13) adds `observations`: what the
consumer itself observed about fields of the node. They are checked here and
handed on beside the body, never inside it: an observation is evidence about
one field, not an entity value, so it must not reach the loop as a fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from ..utils.safe_convert import safe_float
from .domain_yaml_reader import (
    DomainEnrichmentConfig,
    DomainPublicationError,
    DomainYamlReader,
    is_legal_tenant_id,
)
from .spec_types import TypeDeclarationError, describe, node_properties, shape, validate_declaration

REQUEST_CONTRACT_V2 = "eie.converge.request/v2"
V2_MEMBERS = frozenset({"cartridge", "node", "subject", "targets", "observations"})
_OBSERVATION_REQUIRED = frozenset({"field", "value", "source_kind", "source_ref"})
_OBSERVATION_OPTIONAL = frozenset({"unit", "observed_at", "confidence"})
# v1 members a v2 body still carries (request.v2.md section 3.6).
_CARRIED = frozenset(
    {
        "entity",
        "entity_id",
        "object_type",
        "objective",
        "consensus_threshold",
        "max_variations",
        "max_passes",
        "idempotency_key",
    }
)
# v1 selectors: never select in v2, and must agree when present.
_CARTRIDGE_SELECTORS = ("domain", "domain_id", "kb_context")
_NODE_SELECTOR = "node_label"
_REPLACED_BY_TARGETS = ("schema", "field_thresholds")
# Producer context v1.md documents as carried but not interpreted by EIE.
_PRODUCER_CONTEXT = frozenset({"odoo"})
_ALLOWED = (
    V2_MEMBERS
    | _CARRIED
    | _PRODUCER_CONTEXT
    | {"contract", _NODE_SELECTOR, *_CARTRIDGE_SELECTORS, *_REPLACED_BY_TARGETS}
)


class ConvergeRequestRefusedError(ValueError):
    """A v2 request that did not resolve. `code` is a request.v2.md refusal code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


@dataclass(frozen=True)
class ResolvedConvergeRequest:
    """A v2 request resolved against exactly one cartridge revision."""

    body: dict[str, Any]
    config: DomainEnrichmentConfig
    node: str
    subject: dict[str, str] | None
    # Request v2.1: the consumer's observations, validated, in request order.
    # Evidence only (`convergence.observation_evidence`); never part of `body`.
    observations: tuple[dict[str, Any], ...] = ()


def resolve_converge_request_v2(
    tenant: str,
    payload: Mapping[str, Any],
    reader: DomainYamlReader | None,
) -> ResolvedConvergeRequest | None:
    """Resolve a v2 body, return None for a v1 body, or raise ConvergeRequestRefusedError."""
    if "contract" not in payload:
        stray = sorted(V2_MEMBERS & payload.keys())
        if stray:
            raise ConvergeRequestRefusedError(
                "v2_member_without_contract", f"{stray} need contract {REQUEST_CONTRACT_V2!r}"
            )
        return None
    if payload["contract"] != REQUEST_CONTRACT_V2:
        raise ConvergeRequestRefusedError("unsupported_contract", repr(payload["contract"]))
    unknown = sorted(str(key) for key in payload.keys() - _ALLOWED)
    if unknown:
        raise ConvergeRequestRefusedError("invalid_request", f"unknown members {unknown}")
    replaced = [name for name in _REPLACED_BY_TARGETS if name in payload]
    if replaced:
        raise ConvergeRequestRefusedError(
            "conflicting_members", f"{replaced} are replaced by targets"
        )

    cartridge_id, version, digest = _cartridge(payload.get("cartridge"))
    if cartridge_id != tenant:
        raise ConvergeRequestRefusedError(
            "tenant_cartridge_mismatch", f"cartridge {cartridge_id!r} is not tenant {tenant!r}"
        )
    config = _load(reader, cartridge_id)
    _check_pin(config, version, digest)

    node = payload.get("node")
    if not isinstance(node, str) or not node:
        raise ConvergeRequestRefusedError("invalid_request", "node must be a nonempty string")
    declared = node_properties(config.raw_spec, node)
    if node not in config.node_schemas or declared is None:
        raise ConvergeRequestRefusedError(
            "node_not_declared", f"{cartridge_id} declares no node {node!r}"
        )
    _check_selectors(payload, cartridge_id, node)

    schema, thresholds = _targets(payload.get("targets"), {p["name"]: p for p in declared}, node)
    subject = _subject(payload.get("subject"), config.raw_spec, node)
    observations = _observations(payload.get("observations"), {p["name"] for p in declared}, node)

    body = {key: value for key, value in payload.items() if key in _CARRIED | _PRODUCER_CONTEXT}
    body.update(domain_id=cartridge_id, node_label=node, schema=schema)
    if thresholds:
        body["field_thresholds"] = thresholds
    return ResolvedConvergeRequest(
        body=body, config=config, node=node, subject=subject, observations=observations
    )


def _cartridge(value: object) -> tuple[str, str, str | None]:
    if not isinstance(value, Mapping) or value.keys() - {"id", "version", "sha256"}:
        raise ConvergeRequestRefusedError(
            "invalid_request", "cartridge must be {id, version, sha256?}"
        )
    cartridge_id, version, digest = value.get("id"), value.get("version"), value.get("sha256")
    if not is_legal_tenant_id(cartridge_id):
        raise ConvergeRequestRefusedError(
            "invalid_request", f"illegal cartridge id {cartridge_id!r}"
        )
    if not isinstance(version, str) or not version:
        raise ConvergeRequestRefusedError("invalid_request", "cartridge.version is required")
    if digest is not None and (not isinstance(digest, str) or not digest):
        raise ConvergeRequestRefusedError("invalid_request", "cartridge.sha256 must be a string")
    return str(cartridge_id), version, digest


def _load(reader: DomainYamlReader | None, cartridge_id: str) -> DomainEnrichmentConfig:
    if reader is None:
        raise ConvergeRequestRefusedError(
            "cartridge_not_found", "no cartridge reader is configured"
        )
    try:
        return reader.load(cartridge_id)
    except FileNotFoundError as exc:
        raise ConvergeRequestRefusedError("cartridge_not_found", cartridge_id) from exc
    except (DomainPublicationError, ValueError, OSError, yaml.YAMLError) as exc:
        raise ConvergeRequestRefusedError("cartridge_refused", f"{cartridge_id}: {exc}") from exc


def _check_pin(config: DomainEnrichmentConfig, version: str, digest: str | None) -> None:
    if version != config.version:
        raise ConvergeRequestRefusedError(
            "cartridge_pin_mismatch", f"version {version!r} requested, {config.version!r} loaded"
        )
    if digest is None:
        return
    if config.source_sha256 is None:
        raise ConvergeRequestRefusedError(
            "cartridge_pin_mismatch", "sha256 pin cannot be checked: the revision has no digest"
        )
    if digest != config.source_sha256:
        raise ConvergeRequestRefusedError(
            "cartridge_pin_mismatch", "sha256 differs from the loaded revision"
        )


def _check_selectors(payload: Mapping[str, Any], cartridge_id: str, node: str) -> None:
    for name in _CARTRIDGE_SELECTORS:
        if payload.get(name) is not None and payload[name] != cartridge_id:
            raise ConvergeRequestRefusedError(
                "conflicting_members", f"{name} disagrees with cartridge.id"
            )
    if payload.get(_NODE_SELECTOR) is not None and payload[_NODE_SELECTOR] != node:
        raise ConvergeRequestRefusedError("conflicting_members", "node_label disagrees with node")


def _targets(
    value: object, declared: Mapping[str, Mapping[str, Any]], node: str
) -> tuple[dict[str, str | dict[str, Any]], dict[str, float]]:
    """The `schema` and `field_thresholds` the targets resolve to.

    A plain scalar target keeps the v1 type-name form (``"float"``). A ``list``
    or ``map`` target carries its typed declaration (``type`` / ``items`` /
    ``properties`` / ``required``), so validation enforces declared members
    instead of a bare ``"list"`` (F04). An ``enum`` target carries its
    declaration too (``type`` / ``values`` / ``required``): the bare name
    ``"enum"`` loses the allowed values, and validation could then admit any
    value (EIE-D-003).
    """
    if not isinstance(value, list) or not value:
        raise ConvergeRequestRefusedError("invalid_request", "targets must be a nonempty list")
    schema: dict[str, str | dict[str, Any]] = {}
    thresholds: dict[str, float] = {}
    for entry in value:
        if not isinstance(entry, Mapping) or entry.keys() - {"field", "type", "min_confidence"}:
            raise ConvergeRequestRefusedError(
                "invalid_request", "a target is {field, type?, min_confidence?}"
            )
        name = entry.get("field")
        if not isinstance(name, str) or not name:
            raise ConvergeRequestRefusedError(
                "invalid_request", "target.field must be a nonempty string"
            )
        if name in schema:
            raise ConvergeRequestRefusedError("duplicate_target", name)
        declaration = declared.get(name)
        if declaration is None:
            raise ConvergeRequestRefusedError("target_not_declared", f"{node}.{name}")
        if "type" in entry:
            _check_type(name, entry["type"], declaration)
        if "min_confidence" in entry:
            thresholds[name] = _confidence(name, entry["min_confidence"])
        if declaration.get("type") in ("list", "map", "enum"):
            schema[name] = {
                key: declaration[key]
                for key in ("type", "items", "properties", "values", "required")
                if key in declaration
            }
        else:
            schema[name] = str(declaration.get("type"))
    return schema, thresholds


def _check_type(name: str, requested: object, declaration: Mapping[str, Any]) -> None:
    try:
        validate_declaration(requested, f"targets.{name}.type")
    except TypeDeclarationError as exc:
        raise ConvergeRequestRefusedError("invalid_request", str(exc)) from exc
    assert isinstance(requested, Mapping)  # validate_declaration guarantees it
    # `required` is the cartridge's own business, not part of the caller's expectation.
    wanted = {**shape(requested), "required": False}
    actual = {**shape(declaration), "required": False}
    if wanted != actual:
        raise ConvergeRequestRefusedError(
            "target_type_mismatch",
            f"{name}: {describe(requested)} requested, {describe(declaration)} declared",
        )


def _confidence(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ConvergeRequestRefusedError(
            "invalid_request", f"targets.{name}.min_confidence not in [0, 1]"
        )
    return safe_float(value)


def _observations(value: object, declared: set[str], node: str) -> tuple[dict[str, Any], ...]:
    """Validate `observations` (request v2.1). Nothing is defaulted or inferred."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConvergeRequestRefusedError("invalid_request", "observations must be a list")
    return tuple(_observation(entry, declared, node) for entry in value)


def _observation(entry: object, declared: set[str], node: str) -> dict[str, Any]:
    allowed = _OBSERVATION_REQUIRED | _OBSERVATION_OPTIONAL
    if (
        not isinstance(entry, Mapping)
        or entry.keys() - allowed
        or _OBSERVATION_REQUIRED - entry.keys()
    ):
        raise ConvergeRequestRefusedError(
            "invalid_request",
            "an observation is {field, value, source_kind, source_ref, unit?, observed_at?, "
            "confidence?}",
        )
    for key in ("field", "source_kind", "source_ref", *(k for k in ("unit",) if k in entry)):
        if not isinstance(entry[key], str) or not entry[key]:
            raise ConvergeRequestRefusedError(
                "invalid_request", f"observation.{key} must be a nonempty string"
            )
    name = entry["field"]
    if name not in declared:
        raise ConvergeRequestRefusedError("target_not_declared", f"{node}.{name} (observation)")
    if entry["value"] is None:
        raise ConvergeRequestRefusedError(
            "invalid_request", f"observation of {name} carries no value"
        )
    observation = {key: entry[key] for key in entry if key != "confidence"}
    if "observed_at" in entry:
        _check_timestamp(name, entry["observed_at"])
    if "confidence" in entry:
        # The consumer's own statement. EIE never assigns one.
        stated = entry["confidence"]
        if isinstance(stated, bool) or not isinstance(stated, (int, float)) or not 0 <= stated <= 1:
            raise ConvergeRequestRefusedError(
                "invalid_request", f"observation of {name}: confidence not in [0, 1]"
            )
        observation["confidence"] = safe_float(stated)
    return observation


def _check_timestamp(name: str, value: object) -> None:
    try:
        if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("an RFC 3339 timestamp carries a UTC offset")
    except ValueError as exc:
        raise ConvergeRequestRefusedError(
            "invalid_request", f"observation of {name}: observed_at is not RFC 3339"
        ) from exc


def _subject(value: object, spec: Mapping[str, Any], node: str) -> dict[str, str] | None:
    """Check `subject` against the cartridge's `subjects` vocabulary. Opaque strings only."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.keys() - {"type", "role"}:
        raise ConvergeRequestRefusedError("invalid_request", "subject must be {type, role?}")
    subject_type, role = value.get("type"), value.get("role")
    if not isinstance(subject_type, str) or not subject_type:
        raise ConvergeRequestRefusedError(
            "invalid_request", "subject.type must be a nonempty string"
        )
    vocabulary = spec.get("subjects")
    types = vocabulary.get("types") if isinstance(vocabulary, Mapping) else None
    bound = {
        entry["name"]: entry.get("node")
        for entry in (types if isinstance(types, list) else [])
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }
    if subject_type not in bound:
        raise ConvergeRequestRefusedError("subject_not_declared", f"subject.type {subject_type!r}")
    if bound[subject_type] != node:
        raise ConvergeRequestRefusedError(
            "subject_target_mismatch",
            f"{subject_type!r} is bound to {bound[subject_type]!r}, not {node!r}",
        )
    resolved = {"type": subject_type}
    if role is not None:
        roles = vocabulary.get("roles") if isinstance(vocabulary, Mapping) else None
        if not isinstance(role, str) or not isinstance(roles, list) or role not in roles:
            raise ConvergeRequestRefusedError("subject_not_declared", f"subject.role {role!r}")
        resolved["role"] = role
    return resolved


def resolution_echo(resolved: ResolvedConvergeRequest) -> dict[str, Any]:
    """Response v2 revision 1 `resolution`: what EIE actually loaded and pinned against.

    `cartridge_sha256` is the digest of the published spec bytes actually served
    (`DomainEnrichmentConfig.source_sha256`). It is None only for an unpublished
    legacy revision, which a production reader refuses before this point.
    """
    return {
        "cartridge_id": resolved.config.domain_id,
        "cartridge_version": resolved.config.version,
        "cartridge_sha256": resolved.config.source_sha256,
        "node": resolved.node,
    }


def field_declarations(resolved: ResolvedConvergeRequest) -> dict[str, dict[str, Any]]:
    """Response v2 revision 1 `declaration` per field: the node's declarations as loaded.

    The typed shape (`spec_types.shape`) plus the declared `unit` when the
    cartridge states one. Prose such as descriptions is not part of it.
    """
    declarations: dict[str, dict[str, Any]] = {}
    for prop in node_properties(resolved.config.raw_spec, resolved.node) or []:
        declaration = shape(prop)
        if isinstance(prop.get("unit"), str):
            declaration["unit"] = prop["unit"]
        declarations[str(prop["name"])] = declaration
    return declarations


def attach_resolution(result: dict[str, Any], resolved: ResolvedConvergeRequest) -> None:
    """Add `resolution` and each evidence entry's `declaration` to a v2 response body."""
    if result.get("contract_version") is None:
        return
    result["resolution"] = resolution_echo(resolved)
    declarations = field_declarations(resolved)
    for name, entry in (result.get("field_evidence") or {}).items():
        entry["declaration"] = declarations.get(name)
