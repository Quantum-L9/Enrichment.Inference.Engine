"""
Search Parameter Optimizer v2 (search_optimizer_v2.py)

Fully domain-agnostic. Zero hardcoded field maps or industry data.
All domain knowledge injected via DomainClassification from field_classifier.

Deterministic resolver: (mode, target_fields, signals, classification) → SonarConfig

Integration:
  field_classifier.auto_classify_domain(yaml) → DomainClassification
  DomainClassification + EntitySignals → search_optimizer_v2.resolve() → SonarConfig
  SonarConfig → prompt_builder.build_prompt() → payload
  payload → perplexity_client.query_perplexity()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Sonar parameter enums — mirrors the API spec
# ──────────────────────────────────────────────


class SonarModel(StrEnum):
    SONAR = "sonar"
    SONAR_PRO = "sonar-pro"
    SONAR_REASONING = "sonar-reasoning"
    SONAR_REASONING_PRO = "sonar-reasoning-pro"
    SONAR_DEEP_RESEARCH = "sonar-deep-research"


class SearchContextSize(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SearchMode(StrEnum):
    WEB = "web"
    ACADEMIC = "academic"
    SEC = "sec"


class RecencyFilter(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    NONE = "none"


class MessageStrategy(StrEnum):
    SYSTEM_USER = "system_user"
    SYSTEM_USER_ASSISTANT = "system_user_asst"


class FieldDifficulty(StrEnum):
    TRIVIAL = "trivial"
    PUBLIC = "public"
    FINDABLE = "findable"
    OBSCURE = "obscure"
    INFERRABLE = "inferrable"


# ──────────────────────────────────────────────
# Resolved configuration
# ──────────────────────────────────────────────


@dataclass
class SonarConfig:
    """Fully resolved Sonar API parameter set for a single call."""

    model: SonarModel = SonarModel.SONAR
    search_context_size: SearchContextSize = SearchContextSize.MEDIUM
    search_mode: SearchMode = SearchMode.WEB
    recency_filter: RecencyFilter = RecencyFilter.NONE
    domain_filter: list[str] = field(default_factory=list)
    temperature: float = 0.3
    max_tokens: int = 2048
    response_format: str = "json_object"
    message_strategy: MessageStrategy = MessageStrategy.SYSTEM_USER
    variations: int = 3
    reasoning_effort: str | None = None
    disable_search: bool = False
    estimated_cost_per_call: float = 0.0
    resolution_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for TransportPacket.metadata observability."""
        return {
            "model": self.model.value,
            "search_context_size": self.search_context_size.value,
            "search_mode": self.search_mode.value,
            "recency_filter": self.recency_filter.value,
            "domain_filter": self.domain_filter,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": self.response_format,
            "message_strategy": self.message_strategy.value,
            "variations": self.variations,
            "reasoning_effort": self.reasoning_effort,
            "disable_search": self.disable_search,
            "estimated_cost_per_call": self.estimated_cost_per_call,
            "resolution_reason": self.resolution_reason,
        }

    def to_api_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model.value,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "web_search_options": {
                "search_context_size": self.search_context_size.value,
            },
        }
        if self.response_format:
            params["response_format"] = {"type": self.response_format}
        if self.recency_filter != RecencyFilter.NONE:
            params["search_recency_filter"] = self.recency_filter.value
        if self.domain_filter:
            params["search_domain_filter"] = self.domain_filter
        if self.search_mode != SearchMode.WEB:
            params["search_mode"] = self.search_mode.value
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if self.disable_search:
            params["disable_search"] = True
        return params


# ──────────────────────────────────────────────
# Cost model
# ──────────────────────────────────────────────

MODEL_COST_PER_1K: dict[SonarModel, float] = {
    SonarModel.SONAR: 0.0005,
    SonarModel.SONAR_PRO: 0.003,
    SonarModel.SONAR_REASONING: 0.001,
    SonarModel.SONAR_REASONING_PRO: 0.005,
    SonarModel.SONAR_DEEP_RESEARCH: 0.008,
}

CONTEXT_COST_MULTIPLIER: dict[SearchContextSize, float] = {
    SearchContextSize.LOW: 0.6,
    SearchContextSize.MEDIUM: 1.0,
    SearchContextSize.HIGH: 1.8,
}


def estimate_call_cost(
    model: SonarModel,
    context_size: SearchContextSize,
    max_tokens: int,
    variations: int,
) -> float:
    base = MODEL_COST_PER_1K[model]
    ctx_mult = CONTEXT_COST_MULTIPLIER[context_size]
    tokens_per_var = max_tokens * 1.5
    cost_per_var = (tokens_per_var / 1000) * base * ctx_mult
    return round(cost_per_var * variations, 5)


# ──────────────────────────────────────────────
# Entity signals — domain-agnostic
# ──────────────────────────────────────────────


@dataclass
class EntitySignals:
    """Signals extracted from entity state that drive parameter selection.

    Fully agnostic — no field-name awareness. Counts and scores only.
    """

    null_count: int = 0
    known_count: int = 0
    avg_confidence: float = 0.0
    gate_fields_missing: int = 0
    has_website: bool = False
    failed_matches: int = 0
    pass_number: int = 1
    allocated_tokens: int = 5000

    @classmethod
    def from_entity(
        cls,
        entity: dict[str, Any],
        field_map: dict[str, FieldDifficulty],
        confidence_map: dict[str, float] | None = None,
        gate_fields: set[str] | None = None,
        pass_number: int = 1,
        allocated_tokens: int = 5000,
    ) -> EntitySignals:
        non_null = {k: v for k, v in entity.items() if v is not None}
        total_fields = len(field_map)
        conf_map = confidence_map or {}
        avg_conf = sum(conf_map.values()) / max(len(conf_map), 1) if conf_map else 0.0
        gate_missing = 0
        if gate_fields:
            gate_missing = len(gate_fields - set(non_null.keys()))

        return cls(
            null_count=total_fields - len(non_null),
            known_count=len(non_null),
            avg_confidence=avg_conf,
            gate_fields_missing=gate_missing,
            has_website=bool(entity.get("website")),
            failed_matches=int(entity.get("_failed_matches", 0)),
            pass_number=pass_number,
            allocated_tokens=allocated_tokens,
        )


# ──────────────────────────────────────────────
# Resolver internals
# ──────────────────────────────────────────────

DIFFICULTY_RANK: dict[FieldDifficulty, int] = {
    FieldDifficulty.INFERRABLE: 0,
    FieldDifficulty.TRIVIAL: 1,
    FieldDifficulty.PUBLIC: 2,
    FieldDifficulty.FINDABLE: 3,
    FieldDifficulty.OBSCURE: 4,
}


def _dominant_difficulty(
    target_fields: list[str],
    field_map: dict[str, FieldDifficulty],
) -> FieldDifficulty:
    if not target_fields:
        return FieldDifficulty.FINDABLE
    worst = FieldDifficulty.TRIVIAL
    for f in target_fields:
        fd = field_map.get(f, FieldDifficulty.FINDABLE)
        if fd == FieldDifficulty.INFERRABLE:
            continue
        if DIFFICULTY_RANK.get(fd, 3) > DIFFICULTY_RANK.get(worst, 1):
            worst = fd
    return worst


def _needs_reasoning(
    target_fields: list[str],
    signals: EntitySignals,
    ambiguous_fields: set[str],
) -> bool:
    if signals.failed_matches >= 3:
        return True
    if not ambiguous_fields:
        return False
    overlap = set(target_fields) & ambiguous_fields
    return len(overlap) >= 2


def _compute_variations(
    mode: str,
    difficulty: FieldDifficulty,
    signals: EntitySignals,
    budget_tokens: int,
) -> int:
    base = {"discovery": 4, "targeted": 3, "verification": 2}.get(mode, 3)
    if difficulty == FieldDifficulty.OBSCURE:
        base += 1
    elif difficulty == FieldDifficulty.TRIVIAL:
        base = max(base - 1, 2)
    if signals.avg_confidence < 0.4:
        base += 1
    elif signals.avg_confidence > 0.8:
        base = max(base - 1, 2)
    est_tokens_per_var = 1500
    max_by_budget = max(budget_tokens // est_tokens_per_var, 2)
    return min(base, max_by_budget, 7)


def _select_recency(
    target_fields: list[str],
    time_sensitive_fields: set[str],
) -> RecencyFilter:
    if set(target_fields) & time_sensitive_fields:
        return RecencyFilter.YEAR
    return RecencyFilter.NONE


def _select_domain_filters(
    target_fields: list[str],
    field_map: dict[str, FieldDifficulty],
    domain_filters: dict[str, list[str]],
    difficulty: FieldDifficulty,
) -> list[str]:
    if difficulty == FieldDifficulty.OBSCURE:
        return []
    domains: set[str] = set()
    for f in target_fields:
        fd = field_map.get(f, FieldDifficulty.FINDABLE)
        domains.update(domain_filters.get(fd.value, []))
    return sorted(domains)[:5]


def _select_message_strategy(
    mode: str,
    signals: EntitySignals,
) -> MessageStrategy:
    if mode == "discovery":
        return MessageStrategy.SYSTEM_USER
    if signals.known_count > 5:
        return MessageStrategy.SYSTEM_USER_ASSISTANT
    return MessageStrategy.SYSTEM_USER


def _select_max_tokens(mode: str, target_field_count: int) -> int:
    if mode == "verification":
        return max(256, target_field_count * 100)
    if mode == "targeted":
        return max(512, target_field_count * 200)
    return 2048


# ──────────────────────────────────────────────
# Internal resolve helpers
# ──────────────────────────────────────────────


def _pick_model(
    mode: str,
    difficulty: FieldDifficulty,
    signals: EntitySignals,
    amb_fields: set[str],
    searchable: list[str],
    force_model: SonarModel | None,
) -> SonarModel:
    """Select the Sonar model based on mode, difficulty, and signal state."""
    if force_model:
        return force_model
    if mode == "discovery":
        return SonarModel.SONAR_PRO
    if _needs_reasoning(searchable, signals, amb_fields):
        return SonarModel.SONAR_REASONING
    if difficulty == FieldDifficulty.OBSCURE:
        return SonarModel.SONAR_PRO
    if difficulty in (FieldDifficulty.PUBLIC, FieldDifficulty.TRIVIAL):
        return SonarModel.SONAR
    if mode == "targeted" and signals.gate_fields_missing > 0:
        return SonarModel.SONAR_PRO
    return SonarModel.SONAR


def _pick_context_size(mode: str, difficulty: FieldDifficulty) -> SearchContextSize:
    """Map mode/difficulty to the appropriate search context window size."""
    if mode == "discovery" or difficulty == FieldDifficulty.OBSCURE:
        return SearchContextSize.HIGH
    if mode == "verification" or difficulty in (FieldDifficulty.TRIVIAL, FieldDifficulty.PUBLIC):
        return SearchContextSize.LOW
    return SearchContextSize.MEDIUM


def _pick_reasoning_effort(model: SonarModel, signals: EntitySignals) -> str | None:
    """Return reasoning effort level for reasoning-capable models, else None."""
    if model in (SonarModel.SONAR_REASONING, SonarModel.SONAR_REASONING_PRO):
        return "high" if signals.failed_matches >= 3 else "medium"
    return None


def _build_resolution_reason(
    mode: str,
    difficulty: FieldDifficulty,
    searchable: list[str],
    signals: EntitySignals,
    model: SonarModel,
    ctx: SearchContextSize,
    variations: int,
    cost: float,
) -> str:
    """Compose the human-readable resolution reason string for logging and SonarConfig."""
    return (
        f"mode={mode} "
        f"difficulty={difficulty.value} "
        f"fields={len(searchable)} "
        f"null={signals.null_count} "
        f"conf={signals.avg_confidence:.2f} "
        f"gate_missing={signals.gate_fields_missing} "
        f"failed={signals.failed_matches} "
        f"→ {model.value}/{ctx.value} "
        f"×{variations} "
        f"≈${cost:.4f}"
    )


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────


def resolve(
    search_plan_mode: str,
    target_fields: list[str],
    signals: EntitySignals,
    field_map: dict[str, FieldDifficulty],
    domain_filters: dict[str, list[str]] | None = None,
    time_sensitive_fields: set[str] | None = None,
    ambiguous_fields: set[str] | None = None,
    budget_tokens: int | None = None,
    force_model: SonarModel | None = None,
) -> SonarConfig:
    """Deterministic resolver: (mode, fields, signals, classification) → SonarConfig

    All domain knowledge injected via arguments. Zero internal state.

    Args:
        search_plan_mode: "discovery" | "targeted" | "verification"
        target_fields: field names to enrich this pass
        signals: EntitySignals for current entity state
        field_map: {field_name: FieldDifficulty} from DomainClassification
        domain_filters: {difficulty: [domains]} from DomainClassification
        time_sensitive_fields: field names that need recency filter
        ambiguous_fields: field names that trigger reasoning models
        budget_tokens: token budget for this call
        force_model: override model selection
    """
    budget = budget_tokens or signals.allocated_tokens
    d_filters = domain_filters or {}
    ts_fields = time_sensitive_fields or set()
    amb_fields = ambiguous_fields or set()

    # 1. Filter inferrable fields
    searchable = [
        f
        for f in target_fields
        if field_map.get(f, FieldDifficulty.FINDABLE) != FieldDifficulty.INFERRABLE
    ]

    if not searchable:
        return SonarConfig(
            disable_search=True,
            variations=0,
            estimated_cost_per_call=0.0,
            resolution_reason="all_fields_inferrable",
        )

    # 2. Dominant difficulty
    difficulty = _dominant_difficulty(searchable, field_map)

    # 3. Model selection
    model = _pick_model(search_plan_mode, difficulty, signals, amb_fields, searchable, force_model)

    # 4. Context depth
    ctx = _pick_context_size(search_plan_mode, difficulty)

    # 5. Domain filters — from classification, not hardcoded
    domain_filter = _select_domain_filters(
        searchable,
        field_map,
        d_filters,
        difficulty,
    )

    # 6. Recency — from YAML time_sensitive_fields
    recency = _select_recency(searchable, ts_fields)

    # 7. Variations
    variations = _compute_variations(
        search_plan_mode,
        difficulty,
        signals,
        budget,
    )

    # 8. Message strategy
    msg_strategy = _select_message_strategy(search_plan_mode, signals)

    # 9. Max tokens
    max_tokens = _select_max_tokens(search_plan_mode, len(searchable))

    # 10. Temperature
    temperature = {
        "discovery": 0.4,
        "targeted": 0.2,
        "verification": 0.1,
    }.get(search_plan_mode, 0.3)

    # 11. Reasoning effort
    reasoning_effort = _pick_reasoning_effort(model, signals)

    # 12. Cost estimate + reason
    cost = estimate_call_cost(model, ctx, max_tokens, variations)
    reason = _build_resolution_reason(
        search_plan_mode, difficulty, searchable, signals, model, ctx, variations, cost
    )
    logger.info("search_optimizer.resolve", extra={"reason": reason})

    return SonarConfig(
        model=model,
        search_context_size=ctx,
        search_mode=SearchMode.WEB,
        recency_filter=recency,
        domain_filter=domain_filter,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format="json_object",
        message_strategy=msg_strategy,
        variations=variations,
        reasoning_effort=reasoning_effort,
        estimated_cost_per_call=cost,
        resolution_reason=reason,
    )


# ──────────────────────────────────────────────
# Convenience: resolve directly from DomainClassification
# ──────────────────────────────────────────────


def resolve_from_classification(
    search_plan_mode: str,
    target_fields: list[str],
    signals: EntitySignals,
    classification: Any,  # DomainClassification from field_classifier
    budget_tokens: int | None = None,
    force_model: SonarModel | None = None,
) -> SonarConfig:
    """Shorthand: resolve() with DomainClassification unpacked."""
    return resolve(
        search_plan_mode=search_plan_mode,
        target_fields=target_fields,
        signals=signals,
        field_map=classification.field_map,
        domain_filters=classification.domain_filters,
        time_sensitive_fields=classification.time_sensitive_fields,
        ambiguous_fields=classification.ambiguous_fields,
        budget_tokens=budget_tokens,
        force_model=force_model,
    )


# ──────────────────────────────────────────────
# Batch cost estimator
# ──────────────────────────────────────────────


@dataclass
class BatchCostEstimate:
    total_calls: int = 0
    total_variations: int = 0
    estimated_cost: float = 0.0
    breakdown: list[dict[str, Any]] = field(default_factory=list)


def estimate_batch_cost(
    entities: list[dict[str, Any]],
    classification: Any,  # DomainClassification
    passes: int = 3,
) -> BatchCostEstimate:
    """Estimate total cost for enriching a batch of entities."""
    result = BatchCostEstimate()
    fm = classification.field_map

    for entity in entities:
        for pass_num in range(1, passes + 1):
            signals = EntitySignals.from_entity(
                entity,
                field_map=fm,
                gate_fields=classification.gate_fields,
                pass_number=pass_num,
            )
            mode = (
                "discovery"
                if pass_num == 1
                else "verification"
                if pass_num == passes
                else "targeted"
            )
            if mode == "discovery":
                targets = [f for f, d in fm.items() if d != FieldDifficulty.INFERRABLE]
            elif mode == "verification":
                targets = [f for f, d in fm.items() if d == FieldDifficulty.OBSCURE][:3]
            else:
                targets = [
                    f
                    for f, d in fm.items()
                    if d in (FieldDifficulty.OBSCURE, FieldDifficulty.FINDABLE)
                ]

            config = resolve_from_classification(
                mode,
                targets,
                signals,
                classification,
            )
            result.total_calls += 1
            result.total_variations += config.variations
            result.estimated_cost += config.estimated_cost_per_call

    result.estimated_cost = round(result.estimated_cost, 4)
    return result
