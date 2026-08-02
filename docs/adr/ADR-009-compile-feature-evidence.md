# ADR-009: Compile FieldConfidence into FeatureEvidence

## Status
Accepted (TASK-041)

## Context
TASK-019 defined the FeatureEvidence contract. Live convergence still emits
FieldConfidence. Pack schema convergence requires compiling evidence into the
reviewed FeatureEvidence shape without replacing the convergence loop.

## Decision
- Add `compile_field_confidence_map` as the versioned adapter from
  FieldConfidenceMap → list[FeatureEvidence].
- Require rule/model version on inferred evidence (explicit or kb_fragment_ids).
- Ship an Odoo projection fixture (proposal_only) without activating writeback.
- Keep payload free of TransportPacket envelope fields.

## Consequences
Downstream Odoo/CEG consumers can bind to FeatureEvidence while EIE retains
current converge responses. Ranking authority remains out of scope.
