# EIE proposal source attribution

L9_META:
  tier: 3
  domain: convergence
  authority: native-domain-remediation-v2-g27r

## Owner and decision

`app/engines/convergence/schema_proposer.py` owns aggregation of discovery evidence into enrichment-field proposals. Source attribution is evidence provenance, not a source-priority policy. EIE does not author CEG graph gates or ranking policy through this contract.

For a new field that passes the existing fill-rate, average-confidence and canonical-name filters, the proposer must resolve attribution before constructing any `FieldProposal` or returning a `SchemaProposalSet`:

| Distinct accumulated source identifiers | Required behavior |
| --- | --- |
| Zero | Preserve the existing `enrichment` fallback. |
| One | Preserve that exact source identifier through the proposal, YAML diff and materialized property. |
| More than one | Refuse the entire proposal call with `SourceAttributionError`, a `ValueError` subclass. Do not return a partial proposal set or choose a source. |

All qualifying ambiguous fields are retained in `error.field_sources`, a tuple of `(field_name, tuple_of_sources)` pairs sorted by field name and source identifier. The error text uses the same ordering, for example `Ambiguous source attribution: capacity=['enrichment', 'inference']`. Sorting presents evidence only; alphabetical order never selects a winning source. Source frequency, confidence or discovery order also creates no priority rule.

The input batch and current spec are not mutated. Raw competing observations remain in the caller's batch; structured refusal evidence retains all distinct competing source identifiers without copying sample values or confidence payloads into the error. The preflight occurs before candidate materialization in the proposer-to-materializer flow. A caller must preserve the refusal rather than catch it and silently retry with pruned competing evidence.

## Preserved behavior

Existing fields remain excluded using the current supplied ontology semantics. Low-fill, low-confidence and noncanonical/banned fields remain filtered before attribution checking, so ambiguity in a field that cannot qualify does not block valid discoveries. Null-valued observations do not contribute source evidence. Successful proposal ordering and all existing thresholds remain unchanged.

The supported accumulation paths remain flat dictionary entries, legacy `entries` dictionaries and `FieldConfidenceMap`-style records. A dictionary confidence entry with its `source` key absent retains the existing `enrichment` default and therefore counts as enrichment evidence. Numeric confidence entries and object records without a source contribute no explicit source; if the accumulated source set is empty, the field falls back to enrichment. This remediation does not define new malformed-source coercion or validation rules.

## Validation and limits

`tests/unit/test_proposal_source_attribution.py` exercises the actual proposer and `materialize_approved_spec`, including flat/legacy/model representations, input preservation, complete ambiguity evidence, source singleton preservation, no-source fallback and existing/nonqualifying filtering. Fresh subprocesses use `PYTHONHASHSEED=0,1,2,7,42,123,999,random`. They assert the exact refusal evidence and canonical singleton candidate bytes, not merely set iteration order.

The fixed singleton fixture materializes `capacity` as `float` with `source: inference`. Its exact candidate SHA-256 is `c09a8746d50aafeb980ab0b9655c905a52e93b379eb0f67484bde845cc872b14`. This is a synthetic offline fixture digest, not a publication receipt or real review.

This contract does not bind proposals, approvals or historical audit rows to ontology nodes; F-002 is a separate owner decision. Manually constructed proposal sets and persisted historical single-source rows contain no competing-observation set and cannot be certified retrospectively by this preflight. No historical provenance is repaired or inferred. No HTTP error-status contract, database schema, publication workflow, parser policy or CEG schema/boot behavior changes here. A product-owned source-priority policy requires separate explicit review rather than weakening this refusal.
