"""Unit tests for Odoo Gate converge wire-format adapter + handler path."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.engines.handlers import handle_converge
from app.models.schemas import EnrichResponse
from app.services.odoo_gate_converge import (
    PARTNER_WRITEBACK_FIELD_ALLOWLIST,
    build_enrich_request,
    build_odoo_converge_error,
    build_odoo_converge_response,
    filter_partner_fields,
    is_odoo_converge_payload,
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


class TestOdooConvergeAdapter:
    def test_detects_odoo_payload(self) -> None:
        assert is_odoo_converge_payload(_odoo_payload()) is True
        assert is_odoo_converge_payload({"entity_id": "res.partner:1"}) is True
        assert (
            is_odoo_converge_payload(
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
        assert result["status"] == "ok"
        assert result["pass_count"] == 2
        # city already present in snapshot -> omitted (merge-not-overwrite noise)
        assert result["final_fields"] == {"website": "https://acme-new.example"}
        assert result["writeback"]["partner_fields"] == result["final_fields"]
        assert result["total_tokens"] == 1234
        assert result["total_cost_usd"] == 0.05

    def test_error_payload_shape(self) -> None:
        err = build_odoo_converge_error(error="boom", run_id="eie-err")
        assert err["status"] == "error"
        assert err["final_fields"] == {}
        assert err["writeback"]["partner_fields"] == {}
        assert err["error"] == "boom"


@pytest.mark.asyncio
class TestHandleConvergeOdoo:
    async def test_odoo_payload_returns_map_converge_response_shape(self) -> None:
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

        assert result["status"] == "ok"
        assert result["final_fields"]["website"] == "https://enriched.example"
        assert "city" not in result["final_fields"]  # already in snapshot
        assert result["writeback"]["partner_fields"] == result["final_fields"]
        assert result["run_id"].startswith("eie-")
        assert set(result["final_fields"]).issubset(PARTNER_WRITEBACK_FIELD_ALLOWLIST)

        kwargs = loop.await_args.kwargs
        assert kwargs["convergence_config"].max_passes == 2
        assert kwargs["request"].object_type == "res.partner"
        assert "source_urls" in kwargs["request"].entity

    async def test_odoo_parse_error_returns_structured_error(self) -> None:
        result = await handle_converge(
            "plasticos",
            {"entity_snapshot": {"name": "X"}, "domain": "plasticos"},
        )
        assert result["status"] == "error"
        assert "entity_id" in result["error"]
        assert result["final_fields"] == {}

    async def test_timeout_raises_for_odoo_fallback(self) -> None:
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

    async def test_legacy_enrich_request_path_still_works(self) -> None:
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
