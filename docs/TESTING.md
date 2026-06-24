# Testing Guide

## Test Structure

```
tests/
├── unit/                    # Fast, no external deps
│   └── services/enrichment/
│       ├── test_consensus.py
│       ├── test_kb_resolver.py
│       └── test_uncertainty.py
├── integration/             # Requires running services
│   ├── test_consensus_enrichment.py
│   └── test_converge_api.py
├── compliance/              # Architecture and import rules
│   ├── test_architecture.py
│   ├── test_banned_patterns.py
│   └── test_field_names.py
├── ci/                      # Contract validation
│   └── test_repository_contract_calls.py
└── conftest.py              # Shared fixtures
```

## Running Tests

```bash
# All unit tests (fast — no services needed)
pytest tests/unit/ -m unit

# Integration tests (requires Redis + Graphiti + Perplexity key)
pytest tests/integration/ -m integration

# Full suite with coverage
pytest --cov=app --cov-report=term-missing

# Compliance only
pytest tests/compliance/
```

## Coverage Thresholds

| Layer | Minimum |
|-------|---------|
| `app/engines/` | 80% |
| `odoo_modules/` | 70% |
| `app/api/` | 60% |
| Overall | 75% |

CI fails if thresholds are not met.

## Markers

| Marker | When to use |
|--------|-------------|
| `@pytest.mark.unit` | No external deps, < 100ms |
| `@pytest.mark.integration` | Requires live Redis/Graphiti/API |
| `@pytest.mark.slow` | Enrichment or convergence runs (> 5s) |

## Writing Tests for Enrichment Logic

### Signal schema tests
```python
from odoo_modules.plasticos_research_enrichment.models.extraction_engine import ExtractionEngine

def test_validate_rejects_missing_key():
    payload = {"industry_tags": [], "confidence": 0.9}  # missing required keys
    assert ExtractionEngine.validate(payload) is False
```

### Synthesis tests
```python
from odoo_modules.plasticos_research_enrichment.models.synthesis_engine import SynthesisEngine

def test_synthesis_filters_below_threshold():
    payloads = [
        {"industry_tags": ["plastics"], "material_indicators": [], 
         "contamination_flags": [], "compliance_signals": [], "confidence": 0.3}
    ]
    result = SynthesisEngine.synthesize(payloads)
    assert result["signals"] == {}  # below 0.6 threshold
```

## Mocking Perplexity

Use `respx` to mock the Perplexity API in unit tests:

```python
import respx
import httpx

@respx.mock
async def test_perplexity_client_retry(respx_mock):
    respx_mock.post("https://api.perplexity.ai/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": '{"industry_tags": []}'}}]
        })
    )
    # ...
```

## Pre-commit

Run before every commit:
```bash
pre-commit run --all-files
```
