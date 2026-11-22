# Upstream repair request — Gate_SDK and Constellation.Gate (ed25519 signed seam)

Campaign `EIE-CEG-GATE-COLLAB-2026-11-21`, lane PROOF.

This document is the repair brief for the Gate_SDK and Constellation.Gate owners. The operator authorized the repair (CTO answer to Q3, option a), and the CTO assigns the repair lane from this brief. It is not a patch: this lane changes neither repository. Every defect below was observed at the pinned release set, and every receipt cited is in this directory. The rail stays unchanged and re-runs on the repaired release set.

## Release set the defects were observed at

| Component | Coordinate |
|---|---|
| Gate_SDK | `bc1c74c06d70126e82eba73c47df8f2f2af4bc2b`, v1.3.2. This is what Gate, EIE and CEG all pin. SDK code digest `ecd911d5…f929` in every process (`receipts/a8_baseline_main/release_set.ed25519.json`). |
| Gate_SDK head | `dc4e69c4ab99cf5bd4957bbbc49a595409be76b4`. D-1 and D-2 below are byte-identical at head. |
| Constellation.Gate | `b451b68b98b315f57f0c4f9b99090a437e9dac23` |
| EIE / CEG | `aabf855f` / `9d17d01b` |
| Run | `20261122T185228Z-ed25519` (same release set as `20261122T183011Z`). Staging Gate, all six identities ed25519. The observed signing algorithm is `ed25519` in all three containers. |

## Common reproducer and rail

The rail is not changed for any defect below. Build the three images from clean checkouts, then run the named cases:

```bash
gate=<clean Constellation.Gate checkout>/constellation-gate
L9_E2E_WORKSPACE=<dir with the three repos by GitHub name> bash "$gate/tests/e2e/docker/scripts/build_images.sh" all
export GL_GATE_RAIL="$gate/tests/e2e/docker" GL_CEG_REPO=<Cognitive.Engine.Graphs checkout>
python3 tests/e2e/gate_loop/rail.py run --profile ed25519 --cases <cases>
```

Every acceptance run below is that command at the repaired heads. It passes only when:
- all three images were rebuilt from the re-pinned locks;
- `release_set.json` shows one SDK code digest in every process;
- the observed signing algorithm is ed25519 ×3.

**Reading the result.** The command prints one line per case and ends with `{"evidence": "<dir>", "leak_check": "clean"}`. A run passes when `<dir>/summary.json` has `leak_check: "clean"` and every case id the defect names has `verdict: "PASS"`. `BLOCKED`, `FAIL`, `NOT_RUN` and `DIAGNOSTIC_OK` are not passes. The same check as a test: `EIE_E2E_GATE_LOOP=1 GL_CASES=<cases> pytest tests/e2e/gate_loop`, which fails on any non-PASS acceptance id.

"Rail deviation D-n" below means a configuration deviation in `tests/e2e/gate_loop/README.md` (Declared deviations). It is not one of this brief's defects D-1..D-4.

## Lock lines a repaired Gate_SDK release moves

D-1, D-2 and D-3 are Gate_SDK fixes. They ship as one new Gate_SDK commit `<repaired sha>` on the `v1` channel, at the version Gate_SDK releases. All four lock entries below move to that sha in the same change set. No manifest changes, because `pyproject.toml` keeps tag `v1` in EIE (`:104`), CEG (`:84`) and Gate (`constellation-gate/pyproject.toml:37`).

| Repo | File:line at the pinned release | Moves from | Moves to |
|---|---|---|---|
| EIE `aabf855f` | `uv.lock:172-173` | `version = "1.3.2"`, `source … ?tag=v1#bc1c74c06d70126e82eba73c47df8f2f2af4bc2b` | new version, `#<repaired sha>` (`uv lock --upgrade-package constellation-node-sdk`) |
| CEG `9d17d01b` | `uv.lock:180-181` | same two fields | same |
| Gate `b451b68b` | `constellation-gate/requirements.lock:10` | `…/Gate_SDK/archive/bc1c74c0….tar.gz --hash=sha256:cd9d2c20…cd64` | `…/archive/<repaired sha>.tar.gz --hash=sha256:<new archive hash>` |
| Gate `b451b68b` | `constellation-gate/requirements-dev.lock:12` | same line | same |

D-4 is a Gate source fix. Gate is not a dependency of EIE or CEG, so it moves no EIE or CEG lock. It moves the Gate head the rail builds from, and `release_set.json` records the new `org.opencontainers.image.revision`.

## Defects

### D-1 (Gate_SDK) — algorithm-confusion signature forgery (F-A7-03)

**File:line:** Gate_SDK `bc1c74c` `src/constellation_node_sdk/security/verification.py:86-92`, `verify_transport_packet_signature`. The code is byte-identical at SDK head `dc4e69c4`.

**What:** the verifier takes its algorithm from the packet's own `security.signature_algorithm`. A packet that declares `hmac-sha256` gets its `signing_key_id` bytes used as the HMAC secret, even when those bytes are an ed25519 **public** key. Anyone can therefore forge a packet as any ed25519 participant.

**Observed:** Gate answered HTTP 200 to the forged packet. CEG executed `match`, and Gate re-signed the response (`receipts/a7/negatives_main.json`, case `hmac_downgrade_with_public_key`). The control, HMAC with a random secret, was refused with 400.

**Requested repair:** bind each key id to exactly one algorithm. Refuse a packet whose declared algorithm differs from its key's bound algorithm, before any cryptographic check. Never accept ed25519 public-key bytes as HMAC key material. Secondary owner: Gate could enforce an ingress allowlist of algorithms.

**Minimal reproducer:** `--cases negatives`. Today: P11 BLOCKED with `hmac_downgrade_with_public_key` = 200.

**Acceptance test:** `--cases negatives` records P11 PASS. `hmac_downgrade_with_public_key` gets 400 or 403, and every other P11 sub-check is unchanged.

**Re-pin:** the four Gate_SDK lock lines above.

### D-2 (Gate_SDK) — a client resolves its own key id to its private key (F-A7-02)

**File:line:** Gate_SDK `bc1c74c` `src/constellation_node_sdk/gate/config.py:140-151`, `GateClientConfig.resolve_verifying_key`. The code is byte-identical at SDK head `dc4e69c4`.

**What:** for the client's own `signing_key_id` this returns `self.signing_key`, which is correct only for HMAC. Under ed25519, `GateClient._validate_outbound_transport` verifies the client's own packet with its private key, so every ed25519 client fails on send.

**Observed:** `GateSecurityError: outbound packet failed transport validation: TransportAuthenticationError: invalid ed25519 transport signature`. The driver needs rail deviation D-3, a configuration workaround: its own public key in its `GateClient` verifying map. That workaround is not the SDK repair; it only hides this defect from the driver.

**Requested repair:** for ed25519, resolve the client's own key id to its **public** key, derived from the private key, or skip self-verification of the artifact it just signed.

**Minimal reproducer:** remove `key_id: PUB[key_id]` from `verifying_keys` in `tests/e2e/gate_loop/driver/gate_loop_driver.py:74`, which turns rail deviation D-3 off, and run `--cases match`. That case calls `GateClient.execute()` (the consumer `match`). `policy` and `negatives` post raw signed packets and never read the client's verifying map, so they cannot reproduce D-2. At `bc1c74c` every `match` call fails inside the client with the error above, and P33/P34 record NOT_RUN.

**Acceptance test:** at the repaired SDK, with rail deviation D-3 removed, `--cases match` records P33 and P34 PASS: each consumer `match` leaves the client signed, reaches Gate, and is answered with a Gate-verified response.

**Re-pin:** the four Gate_SDK lock lines above. It ships with defect D-3, or node egress still fails its own validation.

### D-3 (Gate_SDK) — ed25519 env layout differs between `GateClient` and the worker runtime (F-A1-04)

**File:line:** Gate_SDK `bc1c74c` `src/constellation_node_sdk/gate/config.py:207`: `GateClient` takes `signing_key` only from `L9_SIGNING_KEY` / `L9_SIGNING_SECRET`. Against that, `src/constellation_node_sdk/runtime/config.py:203-204` refuses `L9_SIGNING_KEY` under ed25519, and `:260` reads `L9_SIGNING_PRIVATE_KEY`.

**What:** a node is both a worker (ingress) and a `GateClient` user (egress), and no single environment satisfies both. EIE's and CEG's egress therefore runs **unsigned** under ed25519, and staging Gate refuses unsigned packets.

**Observed:** EIE's detached `sync` after a converge fails inside EIE with `GateSecurityError … signature required but not present` (`receipts/a8_baseline_main/P07.ed25519.json`, `P05.ed25519.json`). HMAC is no fallback: CEG staging refuses HMAC from Gate (`receipts/a8_baseline_main/hmac_profile_note.json`).

**Requested repair:** under `L9_SIGNING_ALGORITHM=ed25519`, the `GateClient` env reader takes the private key from `L9_SIGNING_PRIVATE_KEY`, the same variable the runtime reads.

**Minimal reproducer:** `--cases converge_plasticos,converge_synthneutral`. Today: P07, P18 and P05 BLOCKED, and the EIE log shows `signature required but not present`.

**Acceptance test:** the same cases record P07, P18 and P05 PASS:
- Gate shows `gate.ingress` for `sync` from `enrichment-engine`, then `gate.completed` with generation ≥ 2;
- the Facility or Station row and its typed child edges are read back from Neo4j.

**Re-pin:** the four Gate_SDK lock lines above, which also re-pins D-2.

### D-4 (Constellation.Gate) — Gate resolves its own key id to its private key (F-A7-01)

**File:line:** Gate `b451b68b` `constellation-gate/src/constellation_gate/config/settings.py:424-435`, `GateSettings.resolve_verifying_key`. It is used as the dispatch key resolver at `constellation-gate/src/constellation_gate/api/dependencies.py:113` and `:118`.

**What:** this is D-2's pattern inside Gate. Gate verifies its own signed dispatch packet with its private key, so under ed25519 every routed call returns 500 (`GateDispatchSecurityError: … invalid ed25519 transport signature`).

**Observed:** every dispatch failed on the first all-ed25519 boot. The rail works around it with rail deviation D-2: Gate's public key in `L9_VERIFYING_KEYS_JSON`, plus the narrowest caller policy staging accepts.

**Requested repair:** resolve Gate's own key id to its public key, or do not self-verify the packet Gate just signed.

**Minimal reproducer:** remove `gate-gl` from `TRUSTED_BY_GATE` in `tests/e2e/gate_loop/scripts/gen_keys.py:31`, which feeds `GL_GATE_VERIFYING_KEYS_JSON` (rail deviation D-2 off), delete `run.env`, and run `--cases policy`. Every routed call returns 500.

**Acceptance test:** with rail deviation D-2 removed (`gate-gl` absent from Gate's verifying map, and its caller-policy entry removed), `--cases policy,converge_plasticos` records P10 PASS. P07 passes too once D-3 also lands.

**Re-pin:** no EIE or CEG lock moves. The Gate head moves, and the rail rebuilds `l9e2e/gate:local` from it. If the D-4 fix lands in the same Gate change as the SDK re-pin, Gate's two lock lines above move in that change too.

## Regression set for every repair

- P03, P09, P10, P13, P17, P19, P33 and P34 stay PASS.
- P12's signer sub-case still returns 502 `worker_response_untrusted`.
- With CEG egress signing, P08 and P22–P30 become runnable on their merits. Their remaining gaps are F-A1-02, F-A1-06 and F-A1-07, owned by the coding lanes.

## What this request does not ask for

- No change to packet fields, actions, payload keys or caller policy semantics.
- No engine-local workaround in EIE or CEG factories. Pack A7.6 forbids it, and it would not repair D-4.
