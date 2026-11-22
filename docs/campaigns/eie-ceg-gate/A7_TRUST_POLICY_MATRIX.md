# A7 — signed participant set, caller policy and trust (baseline at main)

Lane PROOF. Everything here was observed in a real-process run:
- three node images built from clean checkouts, each a separate process;
- real SDK, all-ed25519, Gate in `staging`.

This is **main-baseline evidence** for the CTO, not a final verdict. The rail is `tests/e2e/gate_loop/`.

## 1. Signed participant set

| Process | Image revision | Image id | SDK version | SDK code digest |
|---|---|---|---|---|
| gate | `b451b68b98b3` | `939257c7bc22` | 1.3.2 | `ecd911d5ab28d6d7b246e6b93ab48a2ad0fb2293f1f53c26a900bb42a995f929` (50 files) |
| eie | `aabf855f3f02` | `a5c2a50de8bb` | 1.3.2 | `ecd911d5ab28d6d7b246e6b93ab48a2ad0fb2293f1f53c26a900bb42a995f929` (50 files) |
| ceg | `9d17d01b1367` | `e53854fc2e56` | 1.3.2 | `ecd911d5ab28d6d7b246e6b93ab48a2ad0fb2293f1f53c26a900bb42a995f929` (50 files) |

- **Code digest:** sha256 over the sorted `(relative path, file sha256)` pairs of every `.py` file in the installed `constellation_node_sdk`. It excludes install metadata.
- **Result:** the three processes run byte-identical SDK code. That is compatibility proven by content, not by sharing the tag `v1`.
- **Install route:**
  - Gate installs the SDK from a vendored directory (its lock pins `bc1c74c0`, and `build_images.sh` falls back to vendoring when the archive is unreachable).
  - EIE and CEG install it from `uv.lock` git `bc1c74c0`.
  - Their RECORD hashes differ (`5f2347f2…` vs `8af8a967…`); their code digests do not.
- **Signing:** keys are ephemeral ed25519, generated per run by `scripts/gen_keys.py`. Only fingerprints are recorded.

TEST_ONLY packs (r2):

| Pack | sha256 |
|---|---|
| CEG `plasticos` | `1437177929cd1ccd1a6c6f91fa7f28af1231882f1a561177d81f31d57bd20b6b` |
| CEG `synthneutral` | `d50b9ab2aba2addf0c11b5c6ad3636f102f6e6479262f6b38d526d9f545bbb3e` |
| EIE `plasticos` | `dd09cc9ec8fea4a8766b0f3650cfab83a0b0a894d0e499f9f75405f21ae48cfe` |
| EIE `synthneutral` | `1a37a607c1d6227a383896d8fda0a8b191b9b40a610dc25aace9870904010bc0` |

## 2. Caller-policy matrix

Each cell is one signed packet sent through Gate for tenant `plasticos`. Receipt: `receipts/a7/policy_matrix_main.json`.

| key id (node, kind) | score | health-assess | graph-inference-result | sync | match |
|---|---|---|---|---|---|
| `odoo-gl` (odoo, consumer) | 200 → graph (failure) | 403 not in policy | 403 not in policy | 403 not in policy | 200 → graph (response) |
| `op-gl` (operator, consumer) | 200 → graph (failure) | 200 → graph (failure) | 403 not in policy | 403 not in policy | 200 → graph (response) |
| `eie-gl` (enrichment-engine, worker) | 403 kind rule | 200 → graph (failure) | 403 not in policy | 200 → graph (response) | 200 → graph (response) |
| `ceg-gl` (graph, worker) | 403 kind rule | 403 kind rule | 200 → enrichment-engine (response) | 403 not in policy | 403 not in policy |

What the matrix shows:
- **`score`:** both worker keys are refused by Gate's operator-only kind rule (`ingress_validator.py:211-220`), which fires before the action list is checked; both workers' lists name `score` on purpose. Consumers reach CEG, which answers `failure`, because `REMOTE_SCORE_ENABLED` is off and no `score:read` grant exists. P36 is therefore NOT_APPLICABLE.
- **`health-assess`:** only the `enrichment-engine` worker may originate it; `ceg-gl` is refused by the kind rule. CEG answered the operator's `health_report` probe with `failure`. The exact refusal is in the receipt and is a CEG-W item.
- **`graph-inference-result`:** only `ceg-gl` may originate it. Gate resolves it to `enrichment-engine` (EIE-owned).
- **Registration:** both nodes register live into an empty registry (`GATE_NODE_REGISTRY_PATH` unset). Hijack resistance stays source-level only (F-A1-05); it was not exercised.

## 3. Lineage

Observed on a consumer `converge` (`receipts/a8_baseline_main/P07.ed25519.json`):
- The response is signed `gate-gl` and verifies against Gate's public key.
- `generation` is 2, `root_id` is the caller's packet, and `causation_id` is Gate's dispatch packet.
- `provenance` is `origin_kind=gate`, `resolved_by_gate=true`, `route_kind=external_ingress`, `original_source_node=odoo`.
- The response `source_node` stays the worker (`enrichment-engine`). This matches the frozen A1 §1f: Gate re-signs the response rather than re-deriving it.
- Hop signatures are not verified (`L9_VERIFY_HOP_SIGNATURES` off), so the hop trace is not claimed as non-forged.

## 4. Negatives

Receipt: `receipts/a7/negatives_main.json`.

| case | HTTP | Gate code / outcome |
|---|---|---|
| baseline_valid | 200 | response (Gate-signed) |
| unknown_action | 403 | action_not_permitted |
| wrong_tenant | 403 | action_not_permitted |
| unsigned | 400 | invalid_transport_packet |
| unknown_key_rogue | 400 | invalid_transport_packet |
| corrupted_signature | 400 | invalid_transport_packet |
| key_claims_other_node | 403 | action_not_permitted |
| consumer_claims_registered_node_name | 403 | action_not_permitted |
| hmac_downgrade_with_public_key | 200 | response (Gate-signed) |
| hmac_downgrade_with_random_secret | 400 | invalid_transport_packet |
| replay_first | 200 | response (Gate-signed) |
| replay_second_same_packet_id | 200 | response (Gate-signed) |
| expired_deadline (signed, 1 s deadline, posted after expiry) | 400 | invalid_transport_packet |

The `hmac_downgrade_with_public_key` row is a forgery that Gate **accepted** (**F-A7-03**, CTO-BLOCKER):
- The packet was signed `hmac-sha256` keyed with the consumer's **public** ed25519 key, under that consumer's key id.
- Gate accepted it, CEG executed it, and Gate returned a signed response.
- Root cause: SDK `security/verification.py` (identical at `bc1c74c` and `dc4e69c`) dispatches on the packet-declared algorithm and uses the resolved key bytes as the HMAC secret.
- Owner: Gate_SDK. Gate could also enforce an algorithm allowlist.

CEG's own ingress refuses HMAC from Gate in staging (`receipts/a8_baseline_main/hmac_profile_note.json`).

Replay of a packet id that carries an idempotency key returns the cached response (same `query_id`, no second execution). Without a key, Gate's replay guard returns 400 `replay detected`, observed during the first boot.

## 5. Findings (owners; nothing patched)

| Id | Finding | Owner | Evidence |
|---|---|---|---|
| F-A7-01 | Gate resolves its own key id to its **private** signing key (`config/settings.py:424-435`). Under ed25519 its dispatch packet fails its own outbound validation, so every routed call returns 500. Worked around by D-2. | Constellation.Gate | first ed25519 boot, `gate.failure GateDispatchSecurityError: invalid ed25519 transport signature` |
| F-A7-02 | SDK `GateClientConfig.resolve_verifying_key` (`gate/config.py:140-151`) has the same defect. An ed25519 client fails its own outbound validation. Worked around by D-3 in the driver only. | Gate_SDK | driver `GateSecurityError: outbound packet failed transport validation` |
| F-A7-03 | Algorithm-confusion forgery: an HMAC signature keyed with a public key is accepted. | Gate_SDK (+ Gate allowlist) | negatives `hmac_downgrade_with_public_key` = 200 |
| F-A1-04 (runtime) | EIE and CEG egress cannot sign under ed25519 from env. `GateClient` reads `L9_SIGNING_KEY`, which the worker runtime refuses for ed25519. EIE's detached sync fails with `signature required but not present`. | Gate_SDK (env reader), or EIE-W/CEG-W (factory override) | `P07.ed25519.json`, `P05.ed25519.json` |

Lane rule followed: none of the above is patched locally. D-2 and D-3 are configuration-only, and both are declared.
