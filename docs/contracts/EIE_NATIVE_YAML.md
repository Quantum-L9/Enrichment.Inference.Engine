# EIE native YAML interpretation

`app.engines.native_yaml.parse_native_yaml(bytes_or_text)` is EIE's raw-input
parser. It instantiates a `yaml.SafeLoader` subclass and obtains one safe
document through `get_single_data`. It never rewrites or canonicalizes the input.
Hashing, review receipts, private snapshots and immutable storage continue to
bind the original bytes, not a serialized representation of parsed data.

## Mapping and merge policy

Every mapping, including mappings in sequences, metadata, unused anchors and
rule conditions, must have string keys. Plain YAML scalars resolved as integers,
booleans or null are not string keys; quote them if a literal string is intended.
Explicit duplicate keys are refused before structural validation, even when
their values are identical. Equality uses the decoded string key, including
quoted/escaped spellings, not the original spelling.

One unquoted `<<` merge key per mapping is supported, and its value must be one
mapping, either inline or referenced by an anchor. Acyclic nested single-mapping
merges are supported. Explicit keys override inherited keys regardless of their
position before or after the merge. This inherited-versus-explicit override is
not an explicit duplicate. The source mapping itself must be duplicate-free.
Multiple merge keys, merge sequences (including single-element sequences), and
nonmapping merge values are refused. A quoted `"<<"` without an explicit merge
tag is an ordinary string key. An explicit merge tag is accepted only on the
scalar string `<<`; tagging a nonstring key or another scalar as a merge is
refused. There is no multi-source merge priority policy.

## Safety and resource bounds

The parser accepts one document and only SafeLoader's safe constructors; unsafe
object tags and multiple documents fail. Empty input returns `None`, which the
native structural validator refuses. Malformed YAML raises `yaml.YAMLError`;
policy violations raise its subclass `NativeYamlError`. Callers must not convert
refusal into acceptance of cached or alternative native authority.

| Resource | Maximum |
|---|---:|
| Input bytes (UTF-8 encoded length for text callers) | 2,097,152 |
| Composed and expanded nesting depth, counting root and mapping keys | 64 |
| Composition visits, including keys and alias references | 50,000 |
| Alias references in the document | 128 |
| Expanded graph node occurrences, counting keys and each alias occurrence | 100,000 |

All cyclic aliases are refused, including recursive lists, recursive mappings
and self-merges. Composition limits bound work before Python recursion becomes
unsafe. Pre-construction graph validation detects cycles and checks memoized
expanded size/height before SafeLoader constructs objects or flattens merges.
Noncyclic bounded aliases remain supported. These limits are fixed at the parser
owner and are not runtime bypass flags.

## Supported seams and authority separation

Both `app.engines.domain_yaml_reader` modes use this parser. Published admission
still verifies exact-byte digest, native shape, approval receipt and pointer
stability on each load, before cached snapshots can be returned. A refused
published revision never selects a legacy fixture. Legacy fixture parsing remains
structurally permissive, but no longer accepts ambiguous raw YAML.

`scripts/check_domain_compatibility.py` uses the parser for both EIE raw admission
and CEG data needed for pair diagnostics. That does **not** replace CEG's native
authority: its actual CLI validates a private snapshot of the exact original CEG
bytes. Pair compatibility, reviewed evidence and graph semantics remain separate.

Retained supported loaders `app.services.domain_yaml_reader.DomainSpec.from_yaml`,
`app.engines.convergence.convergence_config.ConvergenceConfig.from_domain_yaml`
and `app.engines.inference.rule_loader.load_rules` also use this parser. The rule
reload path loads and validates before replacing its prior registry. Published
native specs require inline inference rules; references are not approved mutable
authority. Nonproduction references through the protected handler seam remain a
separate supported legacy entry point.

The shared workflow `_native_bytes`, `scan_to_seed` identity extraction and
`materialize_audited_spec` internal reparse now use the same parser. These parent
integration edits preserve the F-004 version policy and the digest of original
input bytes. The protected handler reference-file replacement is still proposed
in `native-remediation/F-003-parser-seams.md`, not applied. Full legacy-reference
seam closure remains pending separately authorized protected-file integration.

## Focused witnesses

`tests/contracts/test_native_yaml_integrity.py` covers duplicate name, version,
sync, properties, type and nested rule condition; hash-bound ambiguous published
bytes; repeated cache/fallback refusal; receipt tampering; canonical-good parsing;
nonstring keys; legitimate explicit merge overrides; rejected merge policies;
cycles and resource bounds; retained legacy loader refusal; and pair exact-byte
snapshot routing. The pair snapshot test is a subprocess routing diagnostic, not
a real CEG native CLI or operator-approval witness. Tests labeled
`parent_workflow_wiring` are integration obligations for the parent, not waived
tests.

L9_META: tier 3; domain enrichment; authority native-domain-remediation-F-003.
