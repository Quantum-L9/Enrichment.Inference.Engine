# Enrichment Package

Multi-source waterfall enrichment with consensus synthesis, uncertainty management, and knowledge base context injection.

## Overview

The enrichment package provides:

- **WaterfallEngine**: Multi-source enrichment with quality-based fallback
- **ConsensusEngine**: Multi-response synthesis from parallel enrichment calls
- **UncertaintyEngine**: Confidence thresholds and risk flagging
- **KBResolver**: Domain knowledge base context injection
- **QualityScorer**: Multi-dimensional quality scoring

## Quick Start

### Standard Enrichment

```python
from app.services.enrichment import WaterfallEngine

engine = WaterfallEngine(perplexity_api_key="pplx-xxx")

merged, quality, results = await engine.enrich(
    domain="company",
    input_payload={"name": "Acme Corp", "domain": "acme.com"},
)
```

### Consensus Enrichment

```python
from app.services.enrichment import WaterfallEngine, KBResolver, UncertaintyConfig

engine = WaterfallEngine(perplexity_api_key="pplx-xxx")
kb_resolver = KBResolver(kb_dir="config/kb")

result = await engine.enrich_with_consensus(
    domain="company",
    input_payload={"name": "Acme Plastics", "type": "company"},
    kb_resolver=kb_resolver,
    kb_context="plastics",
    max_variations=3,
    consensus_threshold=0.65,
    uncertainty_config=UncertaintyConfig(
        low_threshold=0.5,
        high_threshold=0.85,
        critical_threshold=0.3,
    ),
)

print(f"Confidence: {result.confidence}")
print(f"Risk Level: {result.risk_level}")
print(f"Flags: {result.flags}")
```

## Handler API

### `enrich_consensus` Action

**Payload:**

```json
{
    "entity": {
        "name": "Acme Plastics Inc",
        "type": "company",
        "domain": "acmeplastics.com"
    },
    "domain": "company",
    "kb_context": "plastics",
    "max_variations": 3,
    "consensus_threshold": 0.65,
    "uncertainty_thresholds": {
        "low": 0.5,
        "high": 0.85,
        "critical": 0.3
    }
}
```

**Response:**

```json
{
    "fields": {
        "company_name": "Acme Plastics Inc",
        "industry": "Plastics Recycling",
        "employee_count": 150
    },
    "confidence": 0.82,
    "flags": ["moderate_confidence"],
    "variations_attempted": 3,
    "variations_valid": 3,
    "agreement_ratio": 0.89,
    "kb_fragments": ["plastics_polymers", "plastics_grades"],
    "quality_score": 0.78,
    "risk_level": "medium",
    "elapsed_seconds": 2.34
}
```

## Modules

### consensus.py

Synthesizes multiple enrichment responses into a single consensus result.

```python
from app.services.enrichment import synthesize, merge_with_priority

# Synthesize multiple responses
result = synthesize(
    payloads=[response1, response2, response3],
    threshold=0.65,
    total_attempted=3,
)

# Merge consensus into base record
merged = merge_with_priority(
    base=original_entity,
    consensus=result,
    min_agreement=0.5,
)
```

### uncertainty.py

Applies confidence thresholds and generates uncertainty flags.

```python
from app.services.enrichment import apply_uncertainty, UncertaintyConfig

result = apply_uncertainty(
    fields=enriched_fields,
    confidence=0.72,
    field_confidences={"name": 0.9, "industry": 0.6},
    config=UncertaintyConfig(
        low_threshold=0.5,
        high_threshold=0.85,
    ),
)

print(f"Risk Level: {result.risk_level}")
print(f"Flags: {result.flags}")
```

**Risk Levels:**

| Level | Confidence Range | Flags |
|-------|-----------------|-------|
| low | >= high_threshold | (none) |
| medium | >= low_threshold | `moderate_confidence` |
| high | >= critical_threshold | `low_confidence`, `needs_review` |
| critical | < critical_threshold | `critical_low_confidence`, `manual_review_required` |

### kb_resolver.py

Resolves domain knowledge base context for enrichment prompts.

```python
from app.services.enrichment import KBResolver

resolver = KBResolver(kb_dir="config/kb")

context = resolver.resolve(
    kb_context="plastics",
    entity={"name": "Acme Plastics", "type": "company"},
    max_fragments=5,
)

print(f"Domain: {context.domain}")
print(f"Fragments: {context.fragment_ids}")
print(f"Terminology: {context.terminology}")
```

**KB File Format (YAML):**

```yaml
domain: plastics

preamble: |
  Domain knowledge for plastics recycling industry.

terminology:
  HDPE: High-Density Polyethylene
  PP: Polypropylene

entity_hints:
  company:
    typical_fields:
      - company_name
      - polymer_types

fragments:
  - id: plastics_polymers
    content: |
      Common polymer types: HDPE, PP, PET...
    keywords: [polymer, plastics, HDPE]
    entity_types: [company]
    priority: 10
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PERPLEXITY_API_KEY` | Perplexity API key | (required) |
| `KB_DIR` | Knowledge base directory | `config/kb` |

### Waterfall Strategy Config

```yaml
# config/waterfall_config.yaml
waterfall_strategies:
  company:
    max_attempts: 3
    quality_threshold: 0.8
    sources:
      - name: perplexity_sonar
      - name: clearbit
      - name: apollo

fallback_behavior:
  on_quality_below_threshold: use_inference_bridge
```

### Source Enablement & Provider Gating

A source's availability is decided at **three layers**, in order of authority:

| Layer | File | Read at runtime? | Effect of `enabled: true` |
|-------|------|------------------|---------------------------|
| 1. Runtime gate | `config/provider_config.yaml` | **Yes** — `WaterfallEngine` (`waterfall_engine.py`) skips any provider whose `enabled` is not `true` | Provider is eligible for the waterfall **when its `*_API_KEY` env var is also set** |
| 2. Dependency contract | `docs/contracts/dependencies/<provider>.yaml` | Loaded by `runtime_attestation.py` via `_index.yaml`; the `enabled` field itself is **descriptive** (not enforced) | Documents the provider as an intended-active dependency |
| 3. Source catalog | `config/enrichment_sources.yaml` | **No** — not imported by any module; descriptive catalog only | Records catalog intent |

> Enabling a source at layers 2–3 (this change) does **not** by itself cause a live billed API call. The effective gate is layer 1 (`provider_config.yaml`) **plus** a present API key. `zoominfo` and `clay` are already `enabled: true` at layer 1; the other external providers require both their key and a layer-1 flip before they call out.

Activated in this change (documentation/intent flip; no runtime call turned on): `linkedin` (catalog) and the `apollo`, `clearbit`, `hunter`, `zoominfo` dependency contracts.

## Testing

```bash
# Unit tests
pytest tests/unit/services/enrichment/ -v

# Integration tests
pytest tests/integration/test_consensus_enrichment.py -v

# All enrichment tests
pytest -k "enrich" -v
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    handle_enrich_consensus                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      WaterfallEngine                         │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              enrich_with_consensus()                 │   │
│  │                                                      │   │
│  │  1. KB Resolve ──► KBResolver                       │   │
│  │  2. Build Prompts ──► build_variation_prompts()     │   │
│  │  3. Fan-out ──► Perplexity (N parallel)             │   │
│  │  4. Validate ──► _validate_response()               │   │
│  │  5. Consensus ──► synthesize()                      │   │
│  │  6. Uncertainty ──► apply_uncertainty()             │   │
│  │  7. Quality ──► QualityScorer                       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                  ConsensusEnrichmentResult                   │
│  fields, confidence, flags, quality_score, risk_level       │
└─────────────────────────────────────────────────────────────┘
```

## L9 Contract Compliance

This package follows L9 architecture contracts:

- **Contract 1**: No FastAPI imports (chassis-agnostic)
- **Contract 2**: Handler interface: `async def handle_X(tenant, payload) -> dict`
- **Contract 4**: Uses `structlog.get_logger(__name__)` only
- **Contract 12**: Domain spec patterns for configuration
