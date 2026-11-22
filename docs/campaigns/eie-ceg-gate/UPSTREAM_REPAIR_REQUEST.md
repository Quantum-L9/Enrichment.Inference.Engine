# Upstream repair request — Gate_SDK and Constellation.Gate (ed25519 signed seam)

Campaign `EIE-CEG-GATE-COLLAB-2026-11-21`, lane PROOF.

This document requests repairs from the code owners. It is not a patch and it claims no authority: whether to authorize a repair is the operator's decision (CTO answer to Q3). Every defect below was observed at the pinned release set, and every receipt cited is in this directory.

## Release set the defects were observed at

| Component | Coordinate |
|---|---|
| Gate_SDK | `bc1c74c06d70126e82eba73c47df8f2f2af4bc2b`, v1.3.2. This is what Gate, EIE and CEG all pin. SDK code digest `ecd911d5…f929` in every process (`receipts/a8_baseline_main/release_set.ed25519.json`). |
| Gate_SDK head | `dc4e69c4ab99cf5bd4957bbbc49a595409be76b4`. D-1 and D-2 below are byte-identical at head. |
| Constellation.Gate | `b451b68b98b315f57f0c4f9b99090a437e9dac23` |
| EIE / CEG | `aabf855f` / `9d17d01b` |
| Run | `20261122T183011Z-ed25519`. Staging Gate, all six identities ed25519. The observed signing algorithm is `ed25519` in all three containers. |

## Defects

### D-1 (Gate_SDK) — algorithm-confusion signature forgery (F-A7-03)

**Where:** `src/constellation_node_sdk/security/verification.py:86-92`, in `verify_transport_packet_signature`.

**What:** the verifier chooses its algorithm from the packet's own `security.signature_algorithm`. When a packet declares `hmac-sha256`, the bytes resolved for `signing_key_id` become the HMAC secret, even when those bytes are an ed25519 **public** key. Public keys are not secret, so anyone can forge a packet as any ed25519 participant.

**Observed:**
- Gate answered the forged packet with HTTP 200: CEG executed `match`, and Gate re-signed the response (`receipts/a7/negatives_main.json`, case `hmac_downgrade_with_public_key`).
- The control case, an HMAC keyed with a random secret, was refused with 400.

**Requested repair:** bind each key id to exactly one algorithm. The verifier must refuse any packet whose declared algorithm differs from the bound algorithm of its key, before any cryptographic check. Where the mapping is ambient, an ed25519 public key must never be accepted as HMAC key material.

**Secondary owner:** Gate could enforce an allowlist of permitted algorithms at ingress.

### D-2 (Gate_SDK) — a client resolves its own key id to its private key (F-A7-02)

**Where:** `src/constellation_node_sdk/gate/config.py:140-151`, `GateClientConfig.resolve_verifying_key`.

**What:** for the client's own `signing_key_id`, this method returns `self.signing_key`. That only works for symmetric HMAC. Under ed25519, `GateClient._validate_outbound_transport` verifies the client's own packet with its private key, so every ed25519 client fails on send.

**Observed:** the driver failed with `GateSecurityError: outbound packet failed transport validation: TransportAuthenticationError: invalid ed25519 transport signature`. It needed configuration deviation D-3 (its own public key added to its verifying map).

**Requested repair:** for ed25519, resolve the client's own key id to its **public** key, derived from the private key, or skip self-verification of an artifact the client just signed.

### D-3 (Gate_SDK) — ed25519 env layout differs between `GateClient` and the worker runtime (F-A1-04)

**Where:**
- `src/constellation_node_sdk/gate/config.py:207`: `GateClient` takes `signing_key` only from `L9_SIGNING_KEY` or `L9_SIGNING_SECRET`.
- `src/constellation_node_sdk/runtime/config.py:203-204`: the worker runtime refuses `L9_SIGNING_KEY` with ed25519 and reads `L9_SIGNING_PRIVATE_KEY` (`:260`).

**What:** a node is both a worker (ingress) and a `GateClient` user (egress), and no single environment satisfies both. EIE's and CEG's egress clients therefore run **unsigned** under ed25519. Gate in staging refuses unsigned packets, so no node-originated packet leaves the node.

**Observed:**
- EIE's detached `sync` after a converge fails inside EIE with `GateSecurityError … signature required but not present` (`receipts/a8_baseline_main/P07.ed25519.json`, `P05.ed25519.json`).
- HMAC is not a fallback: CEG staging refuses HMAC from Gate with "Gate must sign with ed25519" (`receipts/a8_baseline_main/hmac_profile_note.json`).

**Requested repair:** the `GateClient` env reader takes the ed25519 private key from `L9_SIGNING_PRIVATE_KEY` when `L9_SIGNING_ALGORITHM=ed25519`, which is the same variable the runtime reads. D-2 must be fixed with it, or the client fails its own validation.

### D-4 (Constellation.Gate) — Gate resolves its own key id to its private key (F-A7-01)

**Where:** `constellation-gate/src/constellation_gate/config/settings.py:424-435`, `GateSettings.resolve_verifying_key`. It is used as the dispatch key resolver at `api/dependencies.py:113` and `:118`.

**What:** this is D-2's pattern in Gate. Gate verifies its own signed dispatch packet with its private key, so under ed25519 every routed call returns 500 (`GateDispatchSecurityError: … invalid ed25519 transport signature`).

**Observed:** on the first all-ed25519 boot, every dispatch failed. The rail works around it with configuration deviation D-2: Gate's public key goes in `L9_VERIFYING_KEYS_JSON`, plus the narrowest caller policy staging accepts.

**Requested repair:** same as D-2. Resolve Gate's own id to its public key, or do not self-verify the packet Gate just signed.

## Minimal reproducer

The rail needs no change to reproduce these defects.

```bash
gate=<clean Constellation.Gate checkout>/constellation-gate
L9_E2E_WORKSPACE=<dir with the three repos by GitHub name> bash "$gate/tests/e2e/docker/scripts/build_images.sh" all
export GL_GATE_RAIL="$gate/tests/e2e/docker" GL_CEG_REPO=<Cognitive.Engine.Graphs checkout>
python3 tests/e2e/gate_loop/rail.py run --profile ed25519 --cases negatives,converge_plasticos,converge_synthneutral
```

| Defect | Expected at `bc1c74c` / `b451b68b` |
|---|---|
| D-1 | P11 BLOCKED: `hmac_downgrade_with_public_key` = 200 |
| D-3 | P07, P18 and P05 BLOCKED. The EIE log shows `signature required but not present`. |
| D-4 | Visible after removing `gate-gl` from `GL_GATE_VERIFYING_KEYS_JSON` in `scripts/gen_keys.py` (deviation D-2 off): every call returns 500. |
| D-2 | Visible after removing the driver's own public key from `verifying_keys` in `driver/gate_loop_driver.py` (deviation D-3 off). |

## Acceptance tests the owner fix must pass

Run the unchanged rail at the repaired heads, with all three images rebuilt from repaired locks and the same SDK code digest in every process.

1. **D-1:** `rail.py run --cases negatives` records P11 PASS. `hmac_downgrade_with_public_key` gets 400 or 403, and every other P11 sub-check is unchanged.
2. **D-3 + D-2 (nodes):** `rail.py run --cases converge_plasticos,converge_synthneutral` records P07, P18 and P05 PASS:
   - Gate shows `gate.ingress` for `sync` from `enrichment-engine`, then `gate.completed` with generation ≥ 2.
   - The Facility or Station row and its typed child edges are read back from Neo4j.
3. **D-4 + D-2 (Gate and driver):** the same runs pass with both deviations removed:
   - `gate-gl` absent from Gate's verifying map;
   - no own public key in the driver's verifying map;
   - the `gate-gl` caller policy entry removed.
4. **Regression:** P03, P09, P10, P13, P17, P19, P33 and P34 stay PASS. P12's signer sub-case still returns 502 `worker_response_untrusted`.
5. **Unblocks the loop:** with CEG's egress signing, the P08 / P22–P30 cases become runnable on their merits. Their remaining gaps are F-A1-02, F-A1-06 and F-A1-07, owned by the coding lanes.

## What this request does not ask for

- No change to packet fields, actions, payload keys or caller policy semantics.
- No engine-local workaround in EIE or CEG factories. Pack A7.6 forbids it, and it would not repair D-4.
