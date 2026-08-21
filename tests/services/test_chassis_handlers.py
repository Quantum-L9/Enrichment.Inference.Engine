"""Tests for app.services.chassis_handlers — SDK handler registration bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.chassis_handlers import (
    handle_community_export,
    handle_schema_proposal,
)


class TestHandleCommunityExport:
    @pytest.mark.asyncio
    async def test_empty_communities_returns_zero(self) -> None:
        result = await handle_community_export("tenant-1", {"communities": []})
        assert result["status"] == "ok"
        assert result["targets_queued"] == 0

    @pytest.mark.asyncio
    async def test_missing_communities_key_returns_zero(self) -> None:
        result = await handle_community_export("tenant-1", {})
        assert result["status"] == "ok"
        assert result["targets_queued"] == 0

    @pytest.mark.asyncio
    async def test_valid_communities_builds_envelope(self) -> None:
        communities = [
            {"entity_id": "e1", "community_id": 42},
            {"entity_id": "e2", "community_id": 7},
        ]
        mock_channel = AsyncMock()
        mock_channel.submit.return_value = 2

        with patch("app.services.graph_return_channel.GraphReturnChannel") as mock_cls:
            mock_cls.get_instance.return_value = mock_channel
            result = await handle_community_export("tenant-1", {"communities": communities})
        assert result["status"] == "accepted"
        assert result["targets_queued"] == 2

    @pytest.mark.asyncio
    async def test_filters_incomplete_communities(self) -> None:
        communities = [
            {"entity_id": "e1"},
            {"community_id": 7},
            {"entity_id": "e3", "community_id": 0},
        ]
        mock_channel = AsyncMock()
        mock_channel.submit.return_value = 1

        with patch("app.services.graph_return_channel.GraphReturnChannel") as mock_cls:
            mock_cls.get_instance.return_value = mock_channel
            result = await handle_community_export("tenant-1", {"communities": communities})
        assert result["status"] == "accepted"
        assert result["targets_queued"] == 1


class TestHandleSchemaProposal:
    @pytest.mark.asyncio
    async def test_empty_proposals_returns_zero_count(self) -> None:
        result = await handle_schema_proposal("tenant-1", {"proposed_fields": []})
        assert result["status"] == "received"
        assert result["field_count"] == 0

    @pytest.mark.asyncio
    async def test_missing_proposals_key_returns_zero(self) -> None:
        result = await handle_schema_proposal("tenant-1", {})
        assert result["status"] == "received"
        assert result["field_count"] == 0

    @pytest.mark.asyncio
    async def test_tenant_mismatch_is_rejected(self) -> None:
        result = await handle_schema_proposal(
            "default", {"tenant_id": "override", "proposed_fields": [{"name": "f1"}]}
        )
        assert result["status"] == "rejected"
        assert result["error"] == "tenant_mismatch"
        assert result["tenant_id"] == "default"

    @pytest.mark.asyncio
    async def test_matching_payload_tenant_is_accepted(self) -> None:
        result = await handle_schema_proposal(
            "acme", {"tenant_id": "acme", "proposed_fields": [{"name": "f1"}]}
        )
        assert result["status"] == "received"
        assert result["tenant_id"] == "acme"
        assert result["field_count"] == 1

    @pytest.mark.asyncio
    async def test_packet_id_returned(self) -> None:
        result = await handle_schema_proposal(
            "t1", {"proposed_fields": [{"name": "f1"}], "packet_id": "pkt-123"}
        )
        assert result["packet_id"] == "pkt-123"
