# --- L9_META ---
# l9_schema: 1
# origin: l9-enrich-node
# engine: enrich
# layer: [tests, integration]
# tags: [L9_TEST, gap-validation, ci-bound]
# owner: platform
# status: active
# --- /L9_META ---
"""
tests/integration/test_gap_fixes.py

Validation tests proving all GAP fixes are wired and reachable.
Run: pytest tests/integration/test_gap_fixes.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestResultStoreWiring:
    """GAP #01 — ResultStore is wired: persist_enrich_response delegates to pg_store."""

    def test_result_store_requires_tenant_id(self) -> None:
        from app.services.result_store import ResultStore

        store = ResultStore(tenant_id="acme")
        assert store.tenant_id == "acme"

    @pytest.mark.asyncio
    async def test_persist_enrich_response_calls_pg_store(self) -> None:
        from app.services.result_store import ResultStore

        mock_record = MagicMock()
        mock_record.id = "uuid-123"

        with patch("app.services.result_store.pg_store") as mock_pg:
            mock_pg.save_enrichment_result = AsyncMock(return_value=mock_record)
            store = ResultStore(tenant_id="acme")
            response = MagicMock()
            response.fields = {"industry": "plastics"}
            response.confidence = 0.91
            response.uncertainty_score = 1.2
            response.tokens_used = 3000
            response.processing_time_ms = 500
            response.pass_count = 3
            response.state = "completed"
            response.quality_tier = "high"
            response.inferences = []
            response.kb_fragment_ids = []
            response.feature_vector = None
            response.failure_reason = None

            result_id = await store.persist_enrich_response(
                response,
                entity_id="e1",
                object_type="Account",
            )
            mock_pg.save_enrichment_result.assert_called_once()
            assert result_id == "uuid-123"


class TestConvergeEndpointExecutesController:
    """GAP #03 — /v1/converge invokes run_convergence_loop()."""

    def test_converge_endpoint_wired_to_convergence_controller(self) -> None:
        """Verify the converge_single handler imports and calls run_convergence_loop."""
        import inspect

        from app.api.v1.converge import converge_single

        source = inspect.getsource(converge_single)
        assert "run_convergence_loop" in source

    def test_converge_endpoint_returns_convergence_response_model(self) -> None:
        from app.api.v1.converge import ConvergeSingleResponse

        resp = ConvergeSingleResponse(
            run_id="r1",
            status="converged",
            passes_completed=3,
            fields_discovered=5,
            tokens_used=5000,
            cost_usd=0.025,
            convergence_reason="threshold_met",
        )
        assert resp.passes_completed == 3
        assert resp.status == "converged"


class TestConfigSettings:
    """GAP #04 — Settings defaults and legacy alignment."""

    def test_max_budget_tokens_default(self) -> None:
        from app.core.config import Settings

        s = Settings(perplexity_api_key="")
        assert s.max_budget_tokens == 50_000

    def test_legacy_default_alignment(self) -> None:
        from app.core.config import Settings

        s = Settings(perplexity_api_key="", max_budget_tokens_default=30000)
        assert s.max_budget_tokens == 30000

    def test_explicit_max_budget_wins(self) -> None:
        from app.core.config import Settings

        s = Settings(perplexity_api_key="", max_budget_tokens=8192)
        assert s.max_budget_tokens == 8192


class TestPerplexityApiKeySafeDefault:
    """GAP #05 — missing perplexity_api_key does not crash startup."""

    def test_empty_string_default_does_not_raise(self) -> None:
        from app.core.config import Settings

        s = Settings()
        assert isinstance(s.perplexity_api_key, str)

    def test_key_set_via_env(self, monkeypatch) -> None:
        from app.core.config import Settings

        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test-key-abc123")
        s = Settings()
        assert s.perplexity_api_key == "pplx-test-key-abc123"


class TestCriticalImportPaths:
    """GAP #06 — all new modules import cleanly."""

    def test_result_store_imports(self) -> None:
        from app.services.result_store import ResultStore

        assert callable(getattr(ResultStore, "persist_enrich_response", None))

    def test_packet_router_owns_gate_routed_graph_sync(self) -> None:
        """GAP #22 successor: graph sync is Gate-routed through PacketRouter."""
        from app.engines.packet_router import PacketRouter

        assert callable(getattr(PacketRouter, "notify_graph_sync", None))

    def test_converge_endpoint_imports(self) -> None:
        from app.api.v1.converge import router

        assert router is not None

    def test_config_imports(self) -> None:
        from app.core.config import Settings

        assert callable(Settings)

    def test_converge_router_is_api_router(self) -> None:
        from fastapi import APIRouter

        from app.api.v1.converge import router

        assert isinstance(router, APIRouter)
