# ADR-008: EIE FeatureEvidence Contract

**Status:** Accepted
**Task:** TASK-019
**Date:** 2026-08-02

## Decision

EIE defines `FeatureEvidence` as the attributed, versioned evidence payload.
Inferred values require `provenance.rule_or_model_version`. FieldConfidence remains
the convergence-loop record; adapter maps it into FeatureEvidence.

## Acceptance

All evidence is attributed (source/method/provenance) and versioned
(`execution_version` + rule/model version when inferred).
