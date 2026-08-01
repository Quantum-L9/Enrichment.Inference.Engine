"""
Convergence loop helper functions.

Provides utilities for the convergence controller including:
- Per-field confidence extraction
- Return channel target injection
- Schema proposal emission
- Domain spec enforcement

These helpers are called by the convergence controller during enrichment passes.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from app.utils.safe_convert import safe_float

logger = logging.getLogger(__name__)


def extract_per_field_confidence(feature_vector: dict[str, Any]) -> dict[str, float]:
    """
    Extract per-field confidence scores from a feature vector.

    Falls back through multiple resolution strategies:
    1. Explicit per_field_confidence dict
    2. field_scores nested dict
    3. Flat confidence applied to all non-meta fields
    4. Empty dict (caller treats all fields as uncertain)

    Args:
        feature_vector: Entity feature vector from enrichment pass

    Returns:
        Dict mapping field names to confidence scores (0.0-1.0)
    """
    pfc = feature_vector.get("per_field_confidence")
    if isinstance(pfc, dict) and pfc:
        return {str(k): safe_float(v) for k, v in pfc.items()}

    fs = feature_vector.get("field_scores")
    if isinstance(fs, dict) and fs:
        return {str(k): safe_float(v) for k, v in fs.items()}

    flat = feature_vector.get("confidence") or feature_vector.get("overall_confidence")
    if flat is not None:
        try:
            flat_val = float(flat)
        except (TypeError, ValueError):
            flat_val = 0.0
        meta_keys = {
            "confidence",
            "overall_confidence",
            "pass_number",
            "entity_id",
            "tenant_id",
            "per_field_confidence",
            "field_scores",
        }
        return {k: flat_val for k in feature_vector if k not in meta_keys}

    logger.debug(
        "extract_per_field_confidence: no confidence data found in feature_vector keys=%s",
        list(feature_vector.keys()),
    )
    return {}


async def apply_return_channel_targets(
    entity: dict[str, Any],
    tenant_id: str,
    *,
    timeout: float = 0.05,
) -> dict[str, Any]:
    """
    Drain the GraphReturnChannel for this tenant and inject any
    matching targets as seed values into the entity's known_fields.

    Called at the start of each convergence pass (pass_number >= 2).

    Args:
        entity: Entity dict to potentially update
        tenant_id: Tenant ID for queue lookup
        timeout: Max time to wait for queue drain

    Returns:
        The (possibly updated) entity dict
    """
    from .graph_return_channel import GraphReturnChannel

    channel = GraphReturnChannel.get_instance()
    entity_id = entity.get("entity_id") or entity.get("id")
    targets = await channel.drain(tenant_id=tenant_id, timeout=timeout, max_targets=200)

    matched = 0
    for target in targets:
        if target.entity_id == str(entity_id):
            existing = entity.get(target.field_name)
            if existing is None:
                entity[target.field_name] = target.seed_value
                entity.setdefault("_return_channel_seeds", {})[target.field_name] = {
                    "value": target.seed_value,
                    "source_confidence": target.source_confidence,
                    "origin_rule": target.origin_inference_rule,
                }
                matched += 1

    if matched:
        logger.info(
            "convergence_controller: injected %d return-channel seeds for entity=%s tenant=%s",
            matched,
            entity_id,
            tenant_id,
        )
    return entity


async def emit_schema_proposal(
    proposed_fields: list[dict[str, Any]],
    tenant_id: str,
) -> dict[str, Any]:
    """
    Emit a schema-proposal TransportPacket for newly discovered fields.

    Called from convergence_controller whenever schema_discovery
    produces new field proposals.

    Args:
        proposed_fields: List of field proposal dicts
        tenant_id: Tenant ID

    Returns:
        The emitted packet payload, or empty dict if no fields
    """
    from .contract_enforcement import build_schema_proposal_packet

    if not proposed_fields:
        return {}

    packet = build_schema_proposal_packet(
        tenant_id=tenant_id,
        proposed_fields=proposed_fields,
        provenance="convergence_loop_schema_discovery",
    )

    logger.info(
        "Emitted schema_proposal packet for tenant=%s with %d new fields (packet_id=%s)",
        tenant_id,
        len(proposed_fields),
        packet.header.packet_id,
    )
    return cast("dict[str, Any]", packet.model_dump_json_dict())


class DomainSpecRequiredError(TypeError):
    """Raised when run_convergence_loop is called without domain_spec."""


def enforce_domain_spec(domain_spec: Any, caller: str = "run_convergence_loop") -> None:
    """
    Enforce that domain_spec is provided.

    Domain spec is MANDATORY. Callers that omit it get domain-blind
    enrichment with no sonar optimization.

    Args:
        domain_spec: The domain spec to validate
        caller: Name of calling function for error message

    Raises:
        DomainSpecRequiredError: If domain_spec is None
    """
    if domain_spec is None:
        raise DomainSpecRequiredError(
            f"{caller}() requires domain_spec — omitting it disables domain KB injection "
            f"and sonar optimization. Pass the DomainSpec for the tenant's domain."
        )
