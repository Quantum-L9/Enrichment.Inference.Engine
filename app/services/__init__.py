"""Business logic services: Perplexity client, validation, consensus, KB resolver."""

from .action_authority import (
    ActionAuthorizationError,
    authorize_action,
    authorize_tool,
    evaluate_writeback_payload,
    get_action_policy,
    get_tool_policy,
)
from .crm_field_scanner import (
    CRMField,
    DiscoveryReport,
    DiscoveryReportEntry,
    DomainProperty,
    FieldMapping,
    FieldMatchStatus,
    ImpactTier,
    ScanResult,
    discovery_report_to_dict,
    generate_discovery_report,
    generate_seed_yaml,
    scan_crm_fields,
    scan_result_to_dict,
)
from .dependency_enforcement import (
    ActionDependencyError,
    assert_action_dependencies,
    evaluate_action_dependencies,
)
from .enrichment_profile import (
    DEFAULT_PROFILES,
    EnrichmentProfile,
    EntityBudget,
    EntityRef,
    EntityStore,
    ProfileRegistry,
    SelectionCriteria,
    SelectionMode,
    allocate_budget,
    select_entities,
)
from .event_contract_guard import (
    EventContractError,
    allowed_event_types,
    emit_event,
    to_stream_dict,
    validate_event,
)
from .packet_enforcement import (
    PacketPolicyError,
    PacketValidationError,
    action_policy,
    build_egress_packet,
    canonical_packet_hash,
    registered_actions,
    validate_ingress_packet,
)
from .workers import SchemaPromotionWorker

__all__ = [
    # action_authority.py
    "ActionAuthorizationError",
    "get_action_policy",
    "get_tool_policy",
    "authorize_tool",
    "evaluate_writeback_payload",
    "authorize_action",
    # dependency_enforcement.py
    "ActionDependencyError",
    "evaluate_action_dependencies",
    "assert_action_dependencies",
    # event_contract_guard.py
    "EventContractError",
    "allowed_event_types",
    "validate_event",
    "to_stream_dict",
    "emit_event",
    # packet_enforcement.py
    "PacketValidationError",
    "PacketPolicyError",
    "registered_actions",
    "action_policy",
    "canonical_packet_hash",
    "validate_ingress_packet",
    "build_egress_packet",
    # crm_field_scanner.py
    "FieldMatchStatus",
    "ImpactTier",
    "CRMField",
    "DomainProperty",
    "FieldMapping",
    "ScanResult",
    "DiscoveryReportEntry",
    "DiscoveryReport",
    "scan_crm_fields",
    "generate_seed_yaml",
    "generate_discovery_report",
    "scan_result_to_dict",
    "discovery_report_to_dict",
    # enrichment_profile.py
    "SelectionMode",
    "SelectionCriteria",
    "EnrichmentProfile",
    "EntityRef",
    "EntityBudget",
    "EntityStore",
    "select_entities",
    "allocate_budget",
    "DEFAULT_PROFILES",
    "ProfileRegistry",
    # workers
    "SchemaPromotionWorker",
]
