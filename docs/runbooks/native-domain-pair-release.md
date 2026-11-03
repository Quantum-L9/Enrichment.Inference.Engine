# Native EIE + CEG pair release — local rehearsal only

This candidate keeps two native schemas and two independent artifacts. EIE owns enrichment fields, hints, inline inference/provenance and resolution. CEG owns graph nodes, matching, gates, traversal and scoring. The shared boundary is tenant identity and declared sync method/path/target/idproperty, with field-type checks only at the used intersection. The checker does not rename row fields, infer synonymy, merge YAML or create a new compiler.

## Required inputs and trust

Use complete EIE and CEG candidates, each reviewed as exact bytes by an explicitly identified authorized human. CEG publication input must have a native `-reviewed` or `-production` version suffix. Keep the two artifact versions independent. Retain the native per-artifact approval sidecars and independently audited review evidence. A supplied reviewer name and reference are not cryptographic authentication; protect operator credentials, store write access and immutable history separately.

The operator owns separate roots. Publication tools use atomic per-tenant current pointers and immutable revision directories. This does not make two filesystem stores or PostgreSQL and the filesystem a distributed transaction. Do not switch live traffic to a partial pair. Gate carries no domain-spec delivery action in this campaign, and the Semantic Compiler is not a prerequisite.

## Native pair check

Run in the explicit EIE source environment against the explicit CEG native owner environment:

```bash
.venv/bin/python scripts/check_domain_compatibility.py \
  --eie-spec /absolute/review/eie/spec.yaml \
  --eie-receipt /absolute/review/eie/receipt.json \
  --ceg-spec /absolute/review/ceg/spec.yaml \
  --ceg-receipt /absolute/review/ceg/receipt.json \
  --ceg-repo /absolute/source/Cognitive.Engine.Graphs \
  --ceg-python /absolute/source/Cognitive.Engine.Graphs/.venv/bin/python \
  --output /absolute/review/pair-proof.json
```

The tool runs EIE's native schema validator and CEG's native CLI against an isolated snapshot of the exact hashed input. It requests complete CEG traversal/scoring admission and reviewed-stage admission when both approval sidecars are supplied. Both native owners must pass before the narrow pair check. A release proof binds the two hashes and native versions, review references, the compatibility rule set and proof reference. The proof records code byte hashes separately from checkout HEAD, which may identify a dirty local candidate rather than a committed release.

Exit **0** means native admission, supplied review evidence and compatibility pass. Exit **1** refuses a validation, approval-evidence or compatibility defect. Exit **2** means diagnostic native compatibility passes but real approval evidence is **Unknown**; it is not release-ready. Omitting sidecars is useful for source-fixture diagnostics, never for promotion. An optional `--field-mappings` JSON file supplies explicit per-node source-to-target field-meaning assertions. Their declarations/types are checked, but no transport transformation is implemented by the checker.

## Release and rollback barrier

Before any separately authorized activation, compare the currently selected EIE and CEG artifact hashes with the trusted compatible-pair proof. `app.engines.domain_spec_compatibility.verify_pair_record` rejects a changed hash on either side, a different tenant, an absent proof reference, a wrong rule set or a nonpassing verdict. It is an operator release barrier, not a distributed live admission coordinator. Runtime deployment must ensure both selected artifacts are delivered read-only and cannot independently activate an unproved partial pair.

For rollback, retain the previous exact approved bytes and original per-artifact receipts. Restore both previous compatible artifacts, or validate a deliberately selected mixed old/new pair before activation. Never create a new review timestamp or fake reviewer to label an old artifact approved. Native publishers refuse unknown/tampered rollback targets and stale current-pointer expectations. After restoration, run both native readers and the pair check again; do not equate two successful independent publications with one successful pair release.

## Observed local evidence and deliberate holds

The original PlasticOS source fixtures remain unmodified: EIE `0.3.0` and CEG `1.3.0` have different hashes and all five declared EIE sync references match CEG. Source diagnostics deliberately report approvals Unknown. Tests use derived reviewed-stage graph candidates and isolated test-only receipts to exercise passing and refused releases. Separate local artifact roots, read-only file permissions, pointer reload, receipt/artifact tamper, mismatch and coordinated prior-pair restoration are test evidence only.

Actual Docker/Compose image isolation, real platform read-only mount semantics, CEG protected boot changes, named human authorization and production activation remain separately authorized or Unknown. No deployment, live store promotion, GitHub push, PR, merge or issue mutation is performed by this runbook or this candidate.
