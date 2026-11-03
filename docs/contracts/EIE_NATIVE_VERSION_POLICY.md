# EIE native version publication policy

<!-- L9_META: tier=3; authority=native-domain-remediation-F004 -->

**Executable owner:** `app/services/domain_spec_workflow.py::publish_effective_spec`
**Scope:** ordinary publication of reviewed EIE-native YAML in a trusted local operator store. CEG lifecycle, schema, graph policy, runtime dependencies, and database semantics are unchanged.

## SemVer precedence

Publication uses a private EIE-owned SemVer key. Major, minor, and patch components compare numerically, not lexicographically: `1.9.0 < 1.10.0`. Core numbers use ASCII decimal digits and cannot contain leading zeroes. Numeric prerelease identifiers also cannot contain leading zeroes. Identifiers use ASCII letters, digits, and hyphens.

For equal core components, prerelease is below release. Prerelease identifiers compare left to right: numeric identifiers compare numerically and are below nonnumeric identifiers; nonnumeric identifiers compare in ASCII lexical order. A shorter equal identifier prefix is below a longer one. For example:

```text
1.10.0-alpha < 1.10.0-alpha.1 < 1.10.0-alpha.2
  < 1.10.0-alpha.10 < 1.10.0-alpha.beta < 1.10.0-beta
  < 1.10.0-beta.2 < 1.10.0-beta.11 < 1.10.0-rc.1 < 1.10.0
```

Build metadata is syntactically checked but contributes neither order nor precedence identity. Numeric build identifiers may contain leading zeroes. Thus `1.1.0`, `1.1.0+linux.001`, and `1.1.0+windows.002` have equal precedence.

Numeric comparison uses digit length followed by digit text, avoiding machine-integer or Python string-to-integer conversion limits. No SemVer component, YAML value, or byte artifact is rewritten.

## Ordinary publication under the existing lock

After acquiring the tenant's existing `.publication.lock`, the publisher validates the effective pointer and every retained revision. Each retained entry must have a digest-shaped name and pass the existing no-follow, regular-file, complete-native-spec, exact-digest, receipt-binding, review, and timestamp checks. Unknown names, unfinished staging directories, symlinks, incomplete/corrupt revisions, missing predecessor references, and conflicting historical precedence bindings refuse publication. History is never silently skipped, repaired, deleted, or inferred from audit rows. Even a matching-current retry must pass this history check.

Every retained tenant SemVer **precedence** binds to exactly one SHA-256 of the **original reviewed YAML bytes**. A new candidate with the same precedence and different digest is refused, whether the change is meaningful, whitespace-only, or build-metadata-only. This binding spans all retained history, including revisions that are not current and revisions retained after rollback. Renaming the version with different build metadata cannot evade it.

For a non-idempotent publication, the supplied `expected_previous_sha256` must match the current effective pointer. The candidate's precedence cannot be lower than that current revision's precedence. An already-retained prior digest receives no downgrade exemption: ordinary republication of a lower version is refused, and the explicit rollback API must be used instead. These checks complete before installation or pointer replacement.

The monotonic comparison is against the **current effective revision**, not a separate highest-ever watermark. An explicit rollback changes that comparison baseline but never removes any retained version-to-digest binding. Exact forward restoration of a retained higher-precedence artifact remains possible with matching original review evidence and successful CAS.

## Exact-byte idempotence and receipts

The reviewed-input digest remains SHA-256 over the caller's original `spec_bytes`; bytes are stored unchanged. The full declared version, including build metadata, remains in the original receipt and YAML. There is no normalized serialization, version-only digest, new sidecar schema, or new dependency.

An exact-hash publication that is already current and has identical `reviewed_by`, `approval_ref`, and ordered `approval_batches` remains idempotent: it returns the original receipt without writing anything. Existing behavior permits this no-op even if the caller's expected-previous digest is stale or absent. Conflicting review evidence cannot replace an immutable receipt.

All pointer-changing ordinary operations retain the existing CAS requirement. A retained higher-precedence artifact can be reactivated only with its original matching review evidence; its receipt, review timestamp, approval batches, and original predecessor linkage are not rewritten.

## Explicit approved rollback

`rollback_effective_spec` is unchanged. It restores only an existing structurally valid, digest-bound approved revision, requires current-pointer CAS, and returns its original receipt. It neither creates approval evidence nor edits retained bytes/receipts. A lower version is allowed only through this explicit restoration path, not ordinary publication. The rollback API continues to validate its target and current revision; the all-history uniqueness scan is an ordinary-publication policy, not a new runtime or rollback admission contract.

## Local witnesses and boundary

`tests/integration/test_native_version_policy.py` uses the original complete PlasticOS publication/reader specimen with fixture-only tenant/reviewer identities. It exercises downgrade refusal, retained-hash refusal, meaningful/whitespace conflicts, idempotence, retained precedence conflicts before/after rollback, metadata equality, numeric/prerelease/release order, large numeric identifiers, invalid SemVer, unknown/corrupt history, explicit receipt-preserving rollback, exact forward restoration, and stale CAS. The original `test_native_domain_publication.py` assertions are unchanged.

Focused diagnostics use the repository's existing `.venv/bin/python` and `pytest --no-cov`; this neither changes nor satisfies mandatory whole-repository gates. Real operator authorization, signed approvals, production mounts, database/restart integration, hosted admission, and release readiness are not established by these local fixtures. F-003 parser integration is separately owned; this change does not replace any YAML parser seam.
