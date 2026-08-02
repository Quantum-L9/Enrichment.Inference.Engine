# ADR-010: EIE PlasticOS config is evidence-only

## Status
Accepted (TASK-020)

## Context
`domains/plasticos/spec.yaml` previously declared match gates, scoring
dimensions, and match/outcomes chassis actions. That overlaps CEG ranking
authority.

## Decision
- Remove canonical ranking dimensions and match gates from EIE PlasticOS config.
- Forbid `match` and `outcomes` handler actions in this domain contract.
- Retain ontology, enrichment hints, inference rules, and entity resolution.
- Ranking ownership remains with Cognitive.Engine.Graphs.

## Consequences
EIE emits FeatureEvidence / enrichment outputs; CEG owns candidate ranking.
