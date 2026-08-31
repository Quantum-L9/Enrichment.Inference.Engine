"""Unit tests for the converge dispatch and the compatibility adapter.

Two things are pinned here, and the split between them is the point:

* the live Odoo payload is served by the CANONICAL branch, untranslated;
* the compatibility adapter still handles the older
  ``entity_snapshot`` / top-level ``entity_id`` dialect, and cannot claim a
  canonical payload.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.handlers import handle_converge
from app.models.schemas import EnrichRequest, EnrichResponse
from app.services.odoo_gate_converge import (
    PARTNER_WRITEBACK_FIELD_ALLOWLIST,
    build_enrich_request,
    build_odoo_converge_error,
    build_odoo_converge_response,
    filter_partner_fields,
    is_odoo_compat_converge_payload,
    parse_odoo_converge_request,
)


def _odoo_payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entity_id": "res.partner:55",
        "domain": "plasticos",
        "entity_snapshot": {
            "name": "Acme Recycling",
            "city": "Charlotte",
            "source_urls": ["https://acme.example/about"],
        },
        "odoo": {
            "model": "plasticos.enrichment.run",
            "record_id": 7,
            "correlation_id": "plasticos.enrichment.run:7",
        },
    }
    base.update(overrides)
    return base


def _live_odoo_payload(**overrides: Any) -> dict[str, Any]:
    """Exactly what the live Odoo builder sends.

    Source of truth: IB-Odoo_19 plasticos_gate/services/gate_builders.py
    ::build_converge_request -> gate_contracts.py::ConvergeRequest.to_dict.
    Identity is on the entity; there is no top-level entity_id.
    """
    base: dict[str, Any] = {
        "entity": {
            "name": "Acme Recycling",
            "city": "Charlotte",
            "id": "res.partner:55",
            "_odoo_entity_id": "res.partner:55",
        },
        "object_type": "plasticos",
        "objective": "Full entity enrichment and inference",
        "max_variations": 5,
        "odoo": {"model": "plasticos.enrichment.run", "record_id": 7},
    }
    base.update(overrides)
    return base


class TestLiveOdooBuilderShapeIsCanonical:
    """The live payload is the canonical contract, not a compatibility shape.

    Regression: the compatibility adapter's discriminator was widened to match
    ``entity["id"]``, which is exactly where the live builder puts the canonical
    identity. That routed every production request into an adapter that drops
    that identity, rewrites the caller's objective and object_type, and
    truncates the response fields — a silent contract rewrite.
    """

    def test_live_payload_is_not_a_compatibility_payload(self) -> None:
        assert is_odoo_compat_converge_payload(_live_odoo_payload()) is False

    def test_entity_id_alone_does_not_divert_a_canonical_payload(self) -> None:
        payload = _live_odoo_payload(entity={"name": "Acme", "id": "res.partner:55"})
        assert is_odoo_compat_converge_payload(payload) is False

    def test_compatibility_alias_alone_does_not_divert_it_either(self) -> None:
        payload = _live_odoo_payload(entity={"name": "Acme", "_odoo_entity_id": "res.partner:55"})
        assert is_odoo_compat_converge_payload(payload) is False

    def test_live_payload_validates_as_a_canonical_enrich_request(self) -> None:
        request = EnrichRequest.model_validate(_live_odoo_payload())
        assert request.entity["id"] == "res.partner:55"
        assert request.entity["_odoo_entity_id"] == "res.partner:55"
        assert request.object_type == "plasticos"
        assert request.objective == "Full entity enrichment and inference"
        assert request.max_variations == 5

    def test_internal_enrich_request_is_not_captured(self) -> None:
        assert (
            is_odoo_compat_converge_payload(
                {"entity": {"id": "acme-corp", "name": "Acme"}, "object_type": "Account"}
            )
            is False
        )


class TestOdooConvergeAdapter:
    def test_detects_compatibility_payload(self) -> None:
        assert is_odoo_compat_converge_payload(_odoo_payload()) is True
        assert is_odoo_compat_converge_payload({"entity_id": "res.partner:1"}) is True
        assert (
            is_odoo_compat_converge_payload(
                {
                    "entity": {"Name": "X"},
                    "object_type": "Account",
                    "objective": "enrich",
                }
            )
            is False
        )

    def test_parse_defensive_optional_snapshot_keys(self) -> None:
        parsed = parse_odoo_converge_request(
            {
                "entity_id": "res.partner:9",
                "domain": "plasticos",
                "entity_snapshot": {
                    "name": "Acme",
                    "website": "",
                    "phone": None,
                    "bogus": "drop-me",
                    "source_urls": ["https://a.example", "", None, "https://b.example"],
                },
                "odoo": {"record_id": 1},
                "max_passes": None,
            }
        )
        assert parsed["entity_id"] == "res.partner:9"
        assert parsed["entity_snapshot"] == {
            "name": "Acme",
            "source_urls": ["https://a.example", "https://b.example"],
        }
        assert parsed["max_passes"] == 3
        assert "profile_id" in parsed
        assert parsed["profile_id"] is None

    def test_parse_requires_entity_id(self) -> None:
        with pytest.raises(ValueError, match="entity_id"):
            parse_odoo_converge_request({"domain": "plasticos", "entity_snapshot": {}})

    def test_max_passes_bounded(self) -> None:
        parsed = parse_odoo_converge_request(
            {"entity_id": "res.partner:1", "domain": "plasticos", "max_passes": 99}
        )
        assert parsed["max_passes"] == 10

    def test_build_enrich_request_includes_source_urls_and_schema(self) -> None:
        parsed = parse_odoo_converge_request(_odoo_payload())
        req = build_enrich_request(parsed)
        assert req.object_type == "res.partner"
        assert req.entity["name"] == "Acme Recycling"
        assert req.entity["source_urls"] == ["https://acme.example/about"]
        assert req.schema_ is not None
        assert set(req.schema_) == PARTNER_WRITEBACK_FIELD_ALLOWLIST
        assert req.idempotency_key == "converge:plasticos.enrichment.run:7"

    def test_filter_allowlist_and_skip_existing_snapshot_values(self) -> None:
        filtered = filter_partner_fields(
            {
                "website": "https://new.example",
                "city": "Raleigh",
                "polymer_type": "HDPE",
                "email": "",
                "phone": False,
            },
            snapshot={"city": "Charlotte"},
        )
        assert filtered == {"website": "https://new.example"}

    def test_build_odoo_response_shape(self) -> None:
        parsed = parse_odoo_converge_request(_odoo_payload())
        enrich = EnrichResponse(
            fields={
                "website": "https://acme-new.example",
                "city": "Raleigh",
                "polymer_type": "HDPE",
            },
            pass_count=2,
            tokens_used=1234,
            state="completed",
            feature_vector={"cost_summary": {"total_cost_usd": 0.05}},
        )
        result = build_odoo_converge_response(
            enrich_response=enrich,
            parsed=parsed,
            run_id="eie-test-1",
        )
        assert result["run_id"] == "eie-test-1"
        # Odoo's mapper reads `state` and derives its own status from it.
        assert result["state"] == "completed"
        assert result["pass_count"] == 2
        # city already present in snapshot -> omitted (merge-not-overwrite noise)
        assert result["fields"] == {"website": "https://acme-new.example"}
        assert result["tokens_used"] == 1234
        # `total_cost_usd` is not emitted: Odoo's ConvergeResponse pins it to
        # None as UNAVAILABLE (DNB-006) and never reads it off the wire.
        for obsolete in ("status", "final_fields", "writeback", "total_tokens", "total_cost_usd"):
            assert obsolete not in result

    def test_error_payload_shape(self) -> None:
        """Anything but state="completed" makes Odoo's mapper resolve a non-ok
        status, so the run fails closed to degraded instead of injected."""
        err = build_odoo_converge_error(error="boom", run_id="eie-err")
        assert err["state"] == "failed"
        assert err["failure_reason"] == "boom"
        assert err["fields"] == {}
        for obsolete in ("status", "final_fields", "writeback"):
            assert obsolete not in err


@pytest.mark.asyncio
class TestHandleConvergeCompatibilityBranch:
    async def test_compatibility_payload_returns_map_converge_response_shape(self) -> None:
        enrich = EnrichResponse(
            fields={"website": "https://enriched.example", "city": "Raleigh"},
            pass_count=2,
            tokens_used=100,
            state="completed",
            feature_vector={"cost_summary": {"total_cost_usd": 0.01}},
        )
        with (
            patch(
                "app.engines.handlers.run_convergence_loop",
                new=AsyncMock(return_value=enrich),
            ) as loop,
            patch(
                "app.engines.handlers._persist_and_sync",
                new=AsyncMock(),
            ),
            patch("app.engines.handlers.get_settings", return_value=MagicMock()),
        ):
            result = await handle_converge("plasticos", _odoo_payload(max_passes=2))

        assert result["state"] == "completed"
        assert result["fields"]["website"] == "https://enriched.example"
        assert "city" not in result["fields"]  # already in snapshot
        assert result["run_id"].startswith("eie-")
        assert set(result["fields"]).issubset(PARTNER_WRITEBACK_FIELD_ALLOWLIST)

        kwargs = loop.await_args.kwargs
        assert kwargs["convergence_config"].max_passes == 2
        assert kwargs["request"].object_type == "res.partner"
        assert "source_urls" in kwargs["request"].entity

    async def test_odoo_parse_error_returns_structured_error(self) -> None:
        result = await handle_converge(
            "plasticos",
            {"entity_snapshot": {"name": "X"}, "domain": "plasticos"},
        )
        assert result["state"] == "failed"
        assert "entity_id is required" in result["failure_reason"]
        assert result["fields"] == {}

    async def test_compatibility_timeout_raises_for_odoo_fallback(self) -> None:
        async def _slow(*_a: Any, **_k: Any) -> EnrichResponse:
            raise TimeoutError

        with (
            patch(
                "app.engines.handlers.run_convergence_loop",
                new=AsyncMock(side_effect=_slow),
            ),
            patch("app.engines.handlers.get_settings", return_value=MagicMock()),
            patch(
                "app.engines.handlers.DEFAULT_CONVERGE_TIMEOUT_SECONDS",
                0.01,
            ),
            pytest.raises(TimeoutError, match="converge exceeded"),
        ):
            await handle_converge("plasticos", _odoo_payload())

    async def test_canonical_enrich_request_path_still_works(self) -> None:
        enrich = EnrichResponse(fields={"Industry": "Plastics"}, state="completed")
        payload = {
            "entity": {"Name": "Acme"},
            "object_type": "Account",
            "objective": "enrich",
        }
        with (
            patch(
                "app.engines.handlers.run_convergence_loop",
                new=AsyncMock(return_value=enrich),
            ),
            patch("app.engines.handlers._persist_and_sync", new=AsyncMock()),
            patch("app.engines.handlers.get_settings", return_value=MagicMock()),
        ):
            result = await handle_converge("tenant-a", payload)
        assert result["fields"]["Industry"] == "Plastics"
        assert result["state"] == "completed"
