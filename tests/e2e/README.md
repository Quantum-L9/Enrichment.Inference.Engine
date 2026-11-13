# Odoo `converge` e2e through a real Gate

`client -> Gate -> EIE (enrichment-engine) -> Gate -> client`, in Docker, for the
request shape IB-Odoo_19 `build_converge_request` emits (`contracts/converge_request.json`
plus `kb_context`, `schema`, `idempotency_key`).

This is an **overlay** on Constellation.Gate's rail
(`constellation-gate/tests/e2e/docker/compose.yml`), not a copy of it.

`compose.converge.yml` is the overlay, `run_converge_e2e.sh` boots it and runs
`test_converge_through_gate.py`; each file's header says what it does and why.

## Assertions

1. reply `state == "completed"` (and the reply is a `response` packet signed `gate-e2e`);
2. reply `fields` keys ⊆ request `schema` keys;
3. wall-clock round trip < 25 s;
4. a replay of the same logical operation (same `idempotency_key`) returns an identical payload;
5. a packet Gate did not author, posted straight to EIE's `/v1/execute` from inside EIE's
   container, is refused. EIE's Gate-only boundary (`app/middleware/gate_ingress.py`, bound
   to Gate's key id by `L9_GATE_SIGNING_KEY_IDS=gate-e2e` in the overlay) answers HTTP 403
   naming the missing proof for: non-Gate `origin_kind`, non-Gate source, wrong destination,
   `resolved_by_gate=false`, and a full Gate impersonation that is unsigned. A packet signed
   with a forged key under Gate's key id passes the boundary by design and must be refused by
   the SDK's signature verification (never a `response`).

## Run

```bash
gate=<Constellation.Gate checkout>/constellation-gate
docker build -t l9e2e/gate:local "$gate"
docker build -t l9e2e/eie:local .
L9_E2E_GATE_RAIL="$gate/tests/e2e/docker" bash tests/e2e/run_converge_e2e.sh
```

Where TLS is intercepted by an egress proxy, build with the rail's own script instead
(it adds the proxy CA and, if needed, vendors the Gate SDK). It expects the repositories
side by side under `L9_E2E_WORKSPACE` by their GitHub names, every one of them clean:

```bash
cp "$CA_BUNDLE" "$gate/tests/e2e/docker/ca/ca-bundle.crt"     # git-ignored there
L9_E2E_WORKSPACE=<dir> bash "$gate/tests/e2e/docker/scripts/build_images.sh" gate
L9_E2E_WORKSPACE=<dir> bash "$gate/tests/e2e/docker/scripts/build_images.sh" eie
```

Evidence goes to `$L9_E2E_STATE_DIR/evidence-converge/<UTC>/` (default `/root/l9e2e`).
Credentials are generated per run next to it and never enter the repository.

## Selection

`pytest tests/` does not collect this directory: `conftest.py` ignores `test_*.py`
unless `EIE_E2E=1`. With the flag set and no rail, the tests fail; they never skip.
CI runs them in the `e2e-converge` job of `.github/workflows/ci.yml`. That job runs only when the
Constellation.Gate checkout credential (`secrets.SDK_TOKEN`) is present; without it the job says
so in a warning annotation and the job summary instead of running. It triggers only on pull
requests into `main`.

Declared deviation: EIE runs `L9_ENVIRONMENT=test`, because the deterministic provider is
refused elsewhere (EIE-212-F002). Gate stays `staging` with mandatory signatures.
