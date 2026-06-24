# How to validate these to know you’re ready for phase 5

You are ready for phase 5 when the new validator proves all five truths are simultaneously true, not just individually plausible.

## What these new files do

### `docs/contracts/enforcement/phase5-readiness.yaml`
- This is the readiness spec.
- It defines the exact files that must exist for each truth.
- It also defines the Tier 2 pytest subset that must pass for repo truth.

### `scripts/validate_phase5_readiness.py`
- This is the single entrypoint.
- It validates all five truths in one deterministic run.
- It checks:
  - required files exist
  - constitution and attestation verification pass
  - runtime packet/event/authority/dependency code actually executes
  - writeback governance is actually enforced
  - attestation exposes operational fields
  - review and agent controls really block or pass in the right cases

### `tests/contracts/tier2/test_phase5_readiness_validation.py`
- This locks the validator itself under test.
- It proves the readiness validator remains correct as the repo evolves.

---

## Exact validation sequence

Run these in order.

### 1. Validate constitution + attestation first

```bash
python scripts/l9_contract_control.py verify-constitution
python scripts/l9_contract_control.py verify-attestation
```

You must get success on both before anything else matters.

### 2. Run the phase 5 readiness validator

```bash
python scripts/validate_phase5_readiness.py
```

This prints a markdown readiness report and exits non-zero if you are not ready.

If you want machine-readable output:

```bash
python scripts/validate_phase5_readiness.py --json
```

### 3. Run the validator’s own test

```bash
pytest tests/contracts/tier2/test_phase5_readiness_validation.py -q
```

This proves the readiness gate itself is stable.

### 4. Run the direct support tests behind the validator

```bash
pytest tests/contracts/tier2/test_packet_enforcement_module.py \
 tests/contracts/tier2/test_action_dependency_authority_module.py \
 tests/contracts/tier2/test_event_contract_guard_module.py \
 tests/contracts/tier2/test_l9_contract_runtime_bootstrap.py \
 -q --disable-warnings --maxfail=1
```

### 5. Run the broader existing contract-control subset

```bash
pytest tests/contracts/tier2/test_node_constitution_contract.py \
 tests/contracts/tier2/test_runtime_attestation_contract.py \
 tests/contracts/tier2/test_l9_contract_control.py \
 tests/contracts/tier2/test_phase5_readiness_validation.py \
 -q --disable-warnings --maxfail=1
```

---

## How to interpret the results

The readiness script reports five sections.

### Repo truth

This passes only if:
- all required repo control files exist
- constitution verification passes
- attestation verification passes
- the readiness Tier 2 subset passes

If this fails, you are not ready for phase 5.

### Runtime truth

This passes only if:
- packet enforcement imports and executes
- event contract guard imports and executes
- action authority imports and executes
- dependency evaluation imports and executes
- bootstrap + attestation wiring imports and executes

If this fails, your runtime control system is not actually live.

### Governance truth

This passes only if:
- writeback is still `external_mutation`
- writeback still requires `threshold_or_human`
- the tool still maps to the correct chassis action
- writeback blocks without explicit policy clearance
- writeback succeeds only when constitution-backed conditions are satisfied

If this fails, mutation governance is not strong enough for phase 5.

### Operational truth

This passes only if runtime attestation exposes:
- `contract_digest`
- `dependency_readiness`
- `degraded_modes`
- `policy_mode`

If this fails, you do not yet have the runtime observability substrate needed for phase 5.

### Agent truth

This passes only if:
- gate selection works
- review signal blocks unsafe contract-bound changes
- review signal passes safe co-changed edits

If this fails, coding agents and reviewers are not yet operating inside the control system.

---

## The exact standard for “ready for phase 5”

You are ready only when:
1. `python scripts/validate_phase5_readiness.py` exits with code `0`
2. it reports `Overall ready for phase 5: YES`
3. every truth section is `Passed: YES`
4. the readiness validator test passes
5. the direct runtime control module tests pass
6. constitution + attestation verification pass

That means:
- repo truth is real
- runtime truth is real
- governance truth is real
- operational truth is real
- agent truth is real

At that point, phase 5 stops being architectural intent and becomes a safe execution boundary.
