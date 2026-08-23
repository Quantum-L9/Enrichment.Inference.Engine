"""
Dynamic per-entity prompt builder.

Audit fixes applied:
  - H13: Builds a REAL system prompt with schema definition, field types,
         output format example — not a one-liner.
  - H14: Passes ALL non-empty entity fields, not just name+country.
  - M10: Computes real schema hash from actual schema content.

Search intelligence integration:
  - Accepts optional SonarConfig for message_strategy-aware prompt construction
  - SYSTEM_USER_ASSISTANT strategy injects known fields as assistant context
    (primes the model with prior-pass knowledge for surgical targeting)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

SYSTEM_TEMPLATE = """You are a domain research analyst. Your task is to research
the given entity and extract structured data matching the requested schema.

RULES:
1. Return ONLY valid JSON — no markdown fences, no commentary, no explanation.
2. Your response must be a JSON object with exactly two top-level keys:
   - "confidence": a float between 0.0 and 1.0 indicating your overall certainty
   - "fields": an object containing the requested field values
3. Base answers on verifiable, current facts. If uncertain, lower confidence.
4. For list fields, return arrays of non-empty strings.
5. For boolean fields, return true or false (not strings).
6. If you cannot determine a field value with any confidence, omit it from "fields".
7. Never fabricate data. Absence is better than hallucination.

EXAMPLE OUTPUT FORMAT:
{{"confidence": 0.82, "fields": {{"Industry": "Plastics Recycling", "employee_count": 150}}}}
{kb_context}
TARGET SCHEMA:
{schema_block}"""


def _build_assistant_context(entity: dict[str, Any]) -> str | None:
    """Build an assistant message summarizing known fields from prior passes.

    This primes the model with existing knowledge so targeted passes don't
    re-research what's already known — they focus on gaps.
    """
    non_null = {k: v for k, v in entity.items() if v is not None and v != "" and v != []}
    if not non_null:
        return None

    lines = ["I already have the following verified data for this entity:"]
    for key, value in sorted(non_null.items()):
        if key.startswith("_"):
            continue  # skip internal fields
        str_val = str(value)
        if len(str_val) > 300:
            str_val = str_val[:297] + "..."
        lines.append(f"  {key}: {str_val}")
    lines.append("\nFocus your research on the fields I'm still missing from the target schema.")
    return "\n".join(lines)


def build_prompt(
    entity: dict[str, Any],
    object_type: str,
    objective: str,
    target_schema: dict[str, str] | None = None,
    kb_context_text: str = "",
    model: str = "sonar-reasoning",
    sonar_config=None,
) -> dict:
    """Build a Perplexity chat completion payload tailored to this entity.

    Parameters
    ----------
    sonar_config : SonarConfig | None
        When provided, uses message_strategy to determine prompt structure.
        SYSTEM_USER_ASSISTANT injects known fields as an assistant turn.
    """

    # ── Schema block ─────────────────────────────────
    if target_schema:
        schema_lines = [f"  {k} ({v})" for k, v in target_schema.items()]
        schema_block = "\n".join(schema_lines)
    else:
        schema_block = "  (no schema specified — return any relevant structured data)"

    # ── KB context block ─────────────────────────────
    kb_block = ""
    if kb_context_text:
        kb_block = f"\n\nDOMAIN KNOWLEDGE CONTEXT:\n{kb_context_text}\n"

    system = SYSTEM_TEMPLATE.format(
        kb_context=kb_block,
        schema_block=schema_block,
    )

    # ── Entity description — ALL non-empty fields ────
    entity_lines = []
    for key, value in sorted(entity.items()):
        if value is None or value == "" or value == [] or value == {}:
            continue
        str_val = str(value)
        if len(str_val) > 500:
            str_val = str_val[:497] + "..."
        entity_lines.append(f"  {key}: {str_val}")

    entity_block = "\n".join(entity_lines) if entity_lines else "  (minimal data available)"

    user_content = (
        f"OBJECTIVE: {objective}\n\n"
        f"ENTITY TYPE: {object_type}\n\n"
        f"ENTITY DATA:\n{entity_block}\n\n"
        f"Research this entity thoroughly. Return strictly valid JSON."
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    # ── Message strategy: inject assistant context if applicable ──
    if sonar_config is not None:
        # Import here to avoid circular imports
        from ..engines.search_optimizer import MessageStrategy

        if sonar_config.message_strategy == MessageStrategy.SYSTEM_USER_ASSISTANT:
            assistant_ctx = _build_assistant_context(entity)
            if assistant_ctx:
                messages.append({"role": "assistant", "content": assistant_ctx})

    return {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 4000,
    }


def unwrap_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the enrichment fields from a response built against this prompt.

    The system prompt above instructs the model to answer with an envelope,
    ``{"confidence": <float>, "fields": {...}}``. Anything that consumes a
    completion produced by :func:`build_prompt` has to strip that envelope
    before treating the payload as field values.

    Skipping it has two consequences, and the second is the dangerous one:
    the caller merges the literal keys ``confidence`` and ``fields`` instead
    of the requested values, and any completeness-style quality score sees two
    populated keys and reports a perfect result over zero real fields.

    A response with no dict ``fields`` key is returned unchanged, so a model
    that answers with a flat object still works.

    This lives beside ``build_prompt`` deliberately: the module that defines
    the envelope owns reading it, so a new consumer does not have to
    rediscover the shape. ``validation_engine.validate_response`` and
    ``simulation_bridge`` predate this helper and each implement the same
    unwrap inline.
    """
    fields = raw.get("fields")
    return fields if isinstance(fields, dict) else raw


def build_schema_hash(target_schema: dict[str, str] | None) -> str:
    """Deterministic hash of the target schema for caching/dedup."""
    schema_str = json.dumps(sorted((target_schema or {}).items()), sort_keys=True)
    return hashlib.sha256(schema_str.encode()).hexdigest()[:16]


# ── Variation prompt strategies for consensus enrichment ──────────────────────

VARIATION_STRATEGIES = [
    {
        "name": "direct",
        "prefix": "",
        "suffix": "",
        "temperature": 0.7,
    },
    {
        "name": "verification",
        "prefix": "Verify and cross-check the following entity information. ",
        "suffix": " Double-check all facts against multiple sources.",
        "temperature": 0.5,
    },
    {
        "name": "comprehensive",
        "prefix": "Conduct a comprehensive research analysis of this entity. ",
        "suffix": " Include all available details and context.",
        "temperature": 0.8,
    },
    {
        "name": "focused",
        "prefix": "Focus specifically on the core business attributes. ",
        "suffix": " Prioritize accuracy over completeness.",
        "temperature": 0.4,
    },
    {
        "name": "industry_context",
        "prefix": "Research this entity within its industry context. ",
        "suffix": " Consider market position and competitive landscape.",
        "temperature": 0.7,
    },
]


def build_variation_prompts(
    entity: dict[str, Any],
    object_type: str,
    objective: str = "enrich",
    target_schema: dict[str, str] | None = None,
    kb_context_text: str = "",
    model: str = "sonar-reasoning",
    n: int = 3,
    sonar_config=None,
) -> list[dict]:
    """
    Build N variation prompts for consensus-based enrichment.

    Each variation uses a different prompting strategy to elicit diverse
    responses that can be synthesized into a consensus result.

    Parameters
    ----------
    entity : dict
        Entity data to enrich
    object_type : str
        Type of entity (company, contact, etc.)
    objective : str
        Enrichment objective (default: "enrich")
    target_schema : dict | None
        Target schema for structured output
    kb_context_text : str
        Knowledge base context to inject
    model : str
        Perplexity model to use
    n : int
        Number of variations to generate (max 5)
    sonar_config : SonarConfig | None
        Optional Sonar configuration for message strategy

    Returns
    -------
    list[dict]
        List of N Perplexity chat completion payloads
    """
    n = min(max(n, 1), len(VARIATION_STRATEGIES))
    strategies = VARIATION_STRATEGIES[:n]

    prompts = []
    for strategy in strategies:
        modified_objective = f"{strategy['prefix']}{objective}{strategy['suffix']}"

        payload = build_prompt(
            entity=entity,
            object_type=object_type,
            objective=modified_objective.strip(),
            target_schema=target_schema,
            kb_context_text=kb_context_text,
            model=model,
            sonar_config=sonar_config,
        )

        payload["temperature"] = strategy["temperature"]

        payload["_variation_name"] = strategy["name"]

        prompts.append(payload)

    return prompts


def get_variation_strategy_names() -> list[str]:
    """Return list of available variation strategy names."""
    return [str(s["name"]) for s in VARIATION_STRATEGIES]
