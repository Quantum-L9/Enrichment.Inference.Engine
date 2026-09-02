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


class Settings(BaseSettings):
    perplexity_api_key: str = ""
    perplexity_model: str = "sonar-reasoning"

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

    ceg_base_url: str = "http://localhost:8001"

    # Constellation.Gate listens on 9000 in every shipped Gate deployment asset
    # (its .env.example, compose, terraform and entrypoint); 8080 matched nothing.
    gate_url: str = "http://localhost:9000"
    # Explicit Gate registration (TASK-003). Registration is opt-in and non-fatal
    # to process startup; readiness degrades when it fails. Every deployment
    # manifest in infra/k8s enables it — without registration Gate has no
    # `converge` route and Odoo's enrichment path is a deterministic 404.
    gate_registration_enabled: bool = False
    gate_internal_url: str = ""  # URL the Gate dispatches to; empty → derived default
    # Required by Gate in staging/prod (unauthenticated registration is refused
    # there). Supplied via env / secret, never committed.
    gate_admin_token: str = ""
    # Re-registration cadence. Gate marks a worker unhealthy on a connection
    # failure and only a fresh registration (or its own health re-probe)
    # restores routing; re-registering periodically makes recovery independent
    # of a process restart. 0 disables the loop (a single registration at startup).
    gate_reregistration_interval_seconds: float = 60.0
    # Legacy direct peer URLs retained for backward-compatible config loading only.
    graph_node_url: str = "http://localhost:8001"
    score_node_url: str = "http://localhost:8002"
    route_node_url: str = "http://localhost:8003"
    inter_node_secret: str = "dev-inter-node-secret"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme"

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
    def align_legacy_token_budget(self) -> Settings:
        """If only MAX_BUDGET_TOKENS_DEFAULT was customized, apply it to max_budget_tokens."""
        if self.max_budget_tokens == 50_000 and self.max_budget_tokens_default != 50_000:
            self.max_budget_tokens = self.max_budget_tokens_default
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
