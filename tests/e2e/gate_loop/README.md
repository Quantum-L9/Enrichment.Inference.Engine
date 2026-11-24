# gate_loop — real-process signed EIE + Gate + CEG + Neo4j rail (TEST_ONLY)

The rail for campaign `EIE-CEG-GATE-COLLAB-2026-11-21`, lane PROOF (slices A7 and A8).
It is built by this lane; the CTO runs it at the integrated heads.

The rail is an overlay on Constellation.Gate's Docker rail (`constellation-gate/tests/e2e/docker/compose.yml`):
- EIE and CEG share no network, so a direct peer call is impossible.
- Each node is a separate process running its own image's real startup code. Images are built by that rail's `build_images.sh` from clean checkouts and carry the commit as `org.opencontainers.image.revision`.

## What the overlay changes

| File | Effect |
|---|---|
| `compose.gate_loop.yml` | Every identity uses **ed25519** (`gate-gl`, `eie-gl`, `ceg-gl`, consumer `odoo-gl`, operator `op-gl`, and the untrusted `rogue-gl`). Gate runs `staging` with one caller policy per key. CEG loads only the TEST_ONLY packs (`Cognitive.Engine.Graphs tests/fixtures/test_only_packs`). EIE loads `domains/plasticos` (unmodified) plus `cartridges/synthneutral`. |
| `compose.gate_loop.hmac.yml` | Diagnostic profile, class D. CEG in staging refuses HMAC from Gate, which is correct, so this profile cannot exercise CEG (`receipts/a8_baseline_main/hmac_profile_note.json`). |
| `scripts/gen_keys.py` | Ephemeral keys, written outside the repository (mode 0600). Only key ids and public-key fingerprints are recorded. |

### Declared deviations

Each deviation is a configuration choice; none patches Gate, the SDK or any node.

| Id | Deviation | Finding |
|---|---|---|
| D-1 | EIE runs `L9_ENVIRONMENT=test`, as in `compose.converge.yml`, because the deterministic provider requires it. | F-A1-12 |
| ~~D-2~~ | Retired for the integrated rerun: Gate's verifying map holds only its callers. Gate resolves its own ed25519 key id to its public key since Constellation.Gate 2384923. | F-A7-01 (repaired, D-4) |
| ~~D-3~~ | Retired for the integrated rerun: the driver's `GateClient` lists only Gate's key. Gate_SDK 1.3.3 derives the client's own public key. | F-A7-02 (repaired, SDK D-2) |
| Rig | The driver holds the worker keys only to build key-misassignment negatives (P10), and to push CEG-side fixtures through Gate where EIE's own egress is blocked at main (P17, P19, P33, P34). Every such case says so in its evidence. | — |
| Rig | MIX-ALG-01 recreates the graph worker signing hmac-sha256 (`GL_MIXED_CEG=1`: its own `GL_HMAC_CEG` secret, key id `ceg-hm-gl`) while Gate signs ed25519, then restores the trusted worker. | Gate_SDK 1.3.3 D-1 regression |

The rerun is judged at one Gate_SDK release: `rail.py` compares every process's SDK code digest
with `EXPECTED_SDK` and runs no case on a mismatch (`GL_EXPECT_SDK` names another release
explicitly); it is pinned to 1.3.4. `scripts/rail.sh` regenerates a `run.env` written by an older
key layout. Every verifying key is declared with its algorithm (`L9_VERIFYING_KEY_ALGORITHMS_JSON`
for Gate and both workers, `verifying_key_algorithms` in the driver): the rail's keys are raw
32-byte values, which Gate_SDK 1.3.4 refuses undeclared when signatures are required.

MIX-ALG-01 proves the worker side only: an hmac-sha256 worker must verify Gate's ed25519 dispatch.
PASS means Gate's failure for the request is its own response check; the worker's HTTP 401/403
(the D-1 regression seen in Gate_SDK 1.3.3: every verifying key bound to the node's own
algorithm; repaired in 1.3.4) is FAIL. Gate verifying an
hmac answer is outside Gate's contract: its settings bind every verifying key to one algorithm.

## Run

```bash
gate=<clean Constellation.Gate checkout>/constellation-gate
L9_E2E_WORKSPACE=<dir with the three repos by GitHub name> bash "$gate/tests/e2e/docker/scripts/build_images.sh" all
export GL_GATE_RAIL="$gate/tests/e2e/docker" GL_CEG_REPO=<Cognitive.Engine.Graphs checkout>
python3 tests/e2e/gate_loop/rail.py run --profile ed25519          # boots, runs every case
EIE_E2E_GATE_LOOP=1 pytest tests/e2e/gate_loop                    # same, one test per case
```

`pytest tests/` does not collect this directory unless `EIE_E2E_GATE_LOOP=1` (`conftest.py`). With the flag set, a case passes only when the rail recorded PASS. `BLOCKED`, `FAIL`, `NOT_RUN` and `DIAGNOSTIC_OK` all fail the test; nothing is ever skipped.

## Evidence

Each run writes `$GL_STATE_DIR/evidence/<UTC>-<profile>/` (default `/root/l9e2e/gate_loop`):
- `release_set.json`: image revisions and ids, SDK **code** digest per process, pack sha256s, provider class, live registry.
- `ready.json`
- one `<case>.json` per case: assertion, verdict, reason, packet root and child ids, Neo4j and Postgres reads, logs.
- `summary.json`, including a key leak check.

The baseline at main is copied to `docs/campaigns/eie-ceg-gate/receipts/a8_baseline_main/`.
