"""
Convergence Configuration — Threshold and pass limit settings.

Defines convergence behavior:
  - max_passes: Upper bound on enrichment iterations
  - confidence_threshold: Minimum confidence to consider field "converged"
  - min_improvement_delta: Minimum confidence gain to continue iterating
  - priority_fields: Fields to target first in subsequent passes
  - domain_constraints: Domain-specific convergence rules

Consumed by: convergence_controller.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.engines.native_yaml import parse_native_yaml


@dataclass
class ConvergenceConfig:
    """Configuration for convergence loop behavior."""

    max_passes: int = 3
    confidence_threshold: float = 0.85
    min_improvement_delta: float = 0.05
    priority_fields: list[str] = field(default_factory=list)
    domain_constraints: dict[str, Any] = field(default_factory=dict)
    # Per-run token cap (F-007). It only lowers settings.max_budget_tokens.
    max_budget_tokens: int | None = None
    # D-9 v2: operational U_action above which pass 2+ runs "targeted" rather
    # than "verification" once no field is unresolved. A field below its own
    # threshold keeps the pass targeted whatever U_action is.
    uncertainty_threshold: float = 2.0
    # Optional advanced convergence features (default off — no behavior change)
    synthesize_composites: bool = False
    corrective_retrieval_enabled: bool = False

    @classmethod
    def from_domain_yaml(cls, domain: str) -> ConvergenceConfig:
        """
        Load convergence config from domain YAML.

        Looks for:
          config/domains/{domain}.yaml → convergence_settings key
        """
        domain_path = Path(f"config/domains/{domain}.yaml")
        if not domain_path.exists():
            return cls()

        domain_spec = parse_native_yaml(domain_path.read_bytes())

        convergence_settings = domain_spec.get("convergence_settings", {})

        return cls(
            max_passes=convergence_settings.get("max_passes", 3),
            confidence_threshold=convergence_settings.get("confidence_threshold", 0.85),
            min_improvement_delta=convergence_settings.get("min_improvement_delta", 0.05),
            priority_fields=convergence_settings.get("priority_fields", []),
            domain_constraints=convergence_settings.get("domain_constraints", {}),
            synthesize_composites=convergence_settings.get("synthesize_composites", False),
            corrective_retrieval_enabled=convergence_settings.get(
                "corrective_retrieval_enabled", False
            ),
        )

    @classmethod
    def from_dict(cls, config_dict: dict[str, Any]) -> ConvergenceConfig:
        """Create from API request payload."""
        return cls(
            max_passes=config_dict.get("max_passes", 3),
            confidence_threshold=config_dict.get("confidence_threshold", 0.85),
            min_improvement_delta=config_dict.get("min_improvement_delta", 0.05),
            priority_fields=config_dict.get("priority_fields", []),
            domain_constraints=config_dict.get("domain_constraints", {}),
            synthesize_composites=config_dict.get("synthesize_composites", False),
            corrective_retrieval_enabled=config_dict.get("corrective_retrieval_enabled", False),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for response payload."""
        return {
            "max_passes": self.max_passes,
            "confidence_threshold": self.confidence_threshold,
            "min_improvement_delta": self.min_improvement_delta,
            "priority_fields": self.priority_fields,
            "domain_constraints": self.domain_constraints,
            "synthesize_composites": self.synthesize_composites,
            "corrective_retrieval_enabled": self.corrective_retrieval_enabled,
        }
