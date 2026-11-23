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
| D-2 | Gate's own public key is in Gate's verifying map, with the narrowest caller policy staging accepts. | F-A7-01: Gate resolves its own key id to its private key, so it cannot dispatch under ed25519 without this. |
| D-3 | The driver's `GateClient` lists its own public key. | F-A7-02: the same defect in SDK `GateClientConfig`. |
| Rig | The driver holds the worker keys only to build key-misassignment negatives (P10), and to push CEG-side fixtures through Gate where EIE's own egress is blocked at main (P17, P19, P33, P34). Every such case says so in its evidence. | — |

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
