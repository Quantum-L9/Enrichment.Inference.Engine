"""
Settings — single source of truth for all configuration.
Loaded once at startup from env vars / .env file.

Platform integrations: Prefer the L9 Gate/SDK and TransportPacket actions
for third-party systems. Direct CRM/waterfall env fields below remain for legacy or
transitional code paths; new work should not add bespoke HTTP integrations here.

Integration fix applied (PR#21 merge pass):
    GAP-7: max_budget_tokens added as canonical field. convergence_controller.py
           reads getattr(settings, "max_budget_tokens", ...) and previously fell
           back to 30 000 because only max_budget_tokens_default existed.
           max_budget_tokens_default kept for backward compat.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENRICHMENT_PROVIDERS: frozenset[str] = frozenset({"perplexity", "deterministic"})
# EIE-212-F002: the deterministic source invents numeric and boolean values that
# carry no marker, so the only place it can run is one that serves no real
# tenant. The set matches the SDK's unsigned `dev_mode` environments in
# app/main.py; staging and prod are signed, tenant-facing, and refused.
DETERMINISTIC_PROVIDER_ENVIRONMENTS: frozenset[str] = frozenset({"local", "dev", "test"})


class Settings(BaseSettings):
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar-reasoning"

    # EIE-009: which source `enrich` actually asks. "perplexity" is the live
    # paid provider. "deterministic" computes the answer locally from the entity
    # and the target schema (app/services/deterministic_provider.py), so the
    # whole business chain — enrich -> persist -> Gate -> CEG — is reproducible
    # in CI without provider egress. It is selected explicitly and never as a
    # fallback: a missing key, an outage, or an open circuit must still fail.
    # C-09 (EIE-212-F004): a new application control, so the env name is
    # L9_ENRICHMENT_PROVIDER.
    l9_enrichment_provider: str = "perplexity"
    # The SDK's runtime environment (local|dev|test|staging|prod). The SDK reads
    # it for its own preflight; Settings reads the same variable so the provider
    # guard below can refuse a synthetic source in a tenant-facing environment.
    l9_environment: str = "local"

    api_secret_key: str = ""
    api_key_hash: str = ""

    kb_dir: str = "/app/kb"

    redis_url: str = "redis://localhost:6379/0"

    default_consensus_threshold: float = 0.65
    default_max_variations: int = 5
    default_timeout_seconds: int = 120
    max_concurrent_variations: int = 3
    max_entities_per_batch: int = 50

    odoo_url: str = ""
    odoo_db: str = ""
    odoo_username: str = ""
    odoo_password: str = ""
    crm_mapping_path: str = "config/crm/odoo_mapping.yaml"

    # Legacy / direct CRM & enrichment providers (prefer gate/SDK for new integrations).
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    salesforce_username: str = ""
    salesforce_password: str = ""
    salesforce_security_token: str = ""

    hubspot_access_token: str = ""

    clearbit_api_key: str = ""
    zoominfo_api_key: str = ""
    apollo_api_key: str = ""
    hunter_api_key: str = ""

    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # ── Constellation (Gate-only egress; seam audit 2026-09-02) ──────────
    # Gate is the only peer EIE addresses. There is no direct CEG / GRAPH /
    # SCORE / ROUTE URL and no inter-node shared secret: every outbound packet
    # goes to GATE_URL, signed with the SDK's L9_SIGNING_* material, and Gate
    # resolves the destination by action.
    gate_url: str = "http://localhost:8080"
    # Explicit Gate registration (TASK-003). Registration is non-fatal to process
    # startup; a rejection degrades readiness, it does not stop the node serving.
    #
    # EIE-002: this default read False while .env.example line 103 sets
    # GATE_REGISTRATION_ENABLED=true. A deployment that simply omitted the variable
    # therefore never registered — no Gate route resolved to it — while /health
    # stayed green, because an un-attempted registration is None and only an
    # explicit False degraded. A node that is not registered is not usable, so the
    # default now matches the documented deployment contract, and health reports
    # the registration state by name (see app/main.py health_check).
    gate_registration_enabled: bool = True
    gate_internal_url: str = ""  # URL the Gate dispatches to; empty → derived default
    gate_admin_token: str = ""
    # Seconds between re-registration attempts. Registration is reconciliation,
    # not a one-shot: a Gate that restarts, loses its registry, or was unreachable
    # at this node's startup leaves the node running and unroutable until someone
    # restarts the process. The loop closes that without a restart. 0 disables it.
    # C-09 (EIE-212-F004): env name L9_GATE_REREGISTRATION_INTERVAL_SECONDS.
    l9_gate_reregistration_interval_seconds: float = 300.0
    # CEG `sync` contract projection for post-enrichment graph sync: the CEG sync
    # endpoint suffix and its id property (Cognitive.Engine.Graphs domain spec
    # `sync.endpoints`). Defaults match the plasticos domain.
    graph_sync_entity_type: str = "facilities"
    graph_sync_id_property: str = "facility_id"

    database_url: str = "postgresql+asyncpg://enrich:changeme@localhost:5432/enrich"

    domains_dir: str = "./domains"
    default_domain: str = "plasticos"

    max_budget_tokens: int = 50_000
    max_budget_tokens_default: int = 50_000
    token_rate_usd_per_1k: float = 0.005

    cb_failure_threshold: int = 5
    cb_cooldown_seconds: int = 60

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_enrichment_provider(self) -> Settings:
        """Refuse an unknown provider, and refuse the synthetic one where it could persist.

        EIE-212-F002: `deterministic` marks the strings and lists it invents, but
        an int, a float or a bool cannot carry a prefix, so a synthetic
        ``annual_tonnage`` or ``is_certified`` would be persisted and synced to
        CEG indistinguishable from a researched value. Rather than a convention
        nobody can check at read time, the configuration is rejected at startup
        for every environment that serves tenants.
        """
        if self.l9_enrichment_provider not in ENRICHMENT_PROVIDERS:
            msg = (
                f"L9_ENRICHMENT_PROVIDER={self.l9_enrichment_provider!r} "
                f"is not one of {sorted(ENRICHMENT_PROVIDERS)}"
            )
            raise ValueError(msg)
        if (
            self.l9_enrichment_provider == "deterministic"
            and self.l9_environment not in DETERMINISTIC_PROVIDER_ENVIRONMENTS
        ):
            msg = (
                f"L9_ENRICHMENT_PROVIDER='deterministic' is refused for "
                f"L9_ENVIRONMENT={self.l9_environment!r}: the deterministic source "
                "synthesizes unmarked numeric and boolean values and is permitted only "
                f"in {sorted(DETERMINISTIC_PROVIDER_ENVIRONMENTS)}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def align_legacy_token_budget(self) -> Settings:
        """If only MAX_BUDGET_TOKENS_DEFAULT was customized, apply it to max_budget_tokens."""
        if self.max_budget_tokens == 50_000 and self.max_budget_tokens_default != 50_000:
            self.max_budget_tokens = self.max_budget_tokens_default
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
