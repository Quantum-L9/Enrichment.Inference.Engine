"""Pydantic request/response schemas."""

from .evidence_odoo_mapping import (
    ApplyDecision,
    EvidenceApplyInput,
    EvidenceOdooMappingContract,
    OdooFieldState,
    decide_apply,
    load_evidence_odoo_mapping,
)
from .feature_evidence import (
    EvidenceProvenance,
    EvidenceSource,
    FeatureEvidence,
    TypedValue,
    ValueState,
    compile_field_confidence_map,
    feature_evidence_from_field_confidence,
)
from .field_confidence import (
    FieldConfidence,
    FieldConfidenceMap,
    FieldSource,
    compute_field_confidences,
)
from .loop_schemas import (
    ApprovalMode,
    BatchConvergeRequest,
    BatchConvergeResponse,
    ConvergenceReason,
    ConvergeRequest,
    ConvergeResponse,
    PassContext,
    PassResult,
    SchemaProposal,
)
from .schemas import (
    BatchEnrichRequest,
    BatchEnrichResponse,
    EnrichRequest,
    EnrichResponse,
    HealthCheckResponse,
)

__all__ = [
    # schemas.py
    "EnrichRequest",
    "EnrichResponse",
    "BatchEnrichRequest",
    "BatchEnrichResponse",
    "HealthCheckResponse",
    # field_confidence.py
    "FieldSource",
    "FieldConfidence",
    "FieldConfidenceMap",
    "compute_field_confidences",
    # feature_evidence.py
    "ValueState",
    "EvidenceSource",
    "TypedValue",
    "EvidenceProvenance",
    "FeatureEvidence",
    "feature_evidence_from_field_confidence",
    "compile_field_confidence_map",
    # evidence_odoo_mapping.py
    "ApplyDecision",
    "EvidenceApplyInput",
    "EvidenceOdooMappingContract",
    "OdooFieldState",
    "decide_apply",
    "load_evidence_odoo_mapping",
    # loop_schemas.py
    "ApprovalMode",
    "ConvergeRequest",
    "PassResult",
    "SchemaProposal",
    "ConvergenceReason",
    "ConvergeResponse",
    "BatchConvergeRequest",
    "BatchConvergeResponse",
    "PassContext",
]
