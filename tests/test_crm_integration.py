"""
Tests for CRM integration layer — base classes, Odoo client, field mapper,
and writeback orchestrator.

These tests use mocks for all external I/O (XML-RPC calls).
No real Odoo instance is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.crm.base import CRMCredentials, CRMType, WriteResult
from app.services.crm.field_mapper import FieldMapper
from app.services.crm.odoo_client import OdooClient

# ── CRM Base ──────────────────────────────────────────────────


class TestCRMBase:
    """Test CRM base classes and contracts."""

    def test_crm_type_enum(self):
        assert CRMType.ODOO.value == "odoo"
        assert CRMType.SALESFORCE.value == "salesforce"
        assert CRMType.HUBSPOT.value == "hubspot"

    def test_write_result_success(self):
        result = WriteResult(
            success=True,
            record_id="42",
            fields_written=["name", "email"],
        )
        assert result.success is True
        assert result.record_id == "42"
        assert result.fields_written == ["name", "email"]
        assert result.error == ""

    def test_write_result_failure(self):
        result = WriteResult(success=False, error="connection_timeout")
        assert result.success is False
        assert result.error == "connection_timeout"
        assert result.record_id == ""

    def test_crm_credentials(self):
        creds = CRMCredentials(
            crm_type=CRMType.ODOO,
            credentials={"url": "https://odoo.test", "db": "test"},
        )
        assert creds.crm_type == CRMType.ODOO
        assert creds.credentials["url"] == "https://odoo.test"


# ── Odoo Client ───────────────────────────────────────────────


class TestOdooClient:
    """Test Odoo XML-RPC client with mocked transport."""

    @pytest.fixture
    def odoo_creds(self):
        return CRMCredentials(
            crm_type=CRMType.ODOO,
            credentials={
                "url": "https://odoo.test",
                "db": "testdb",
                "username": "admin",
                "password": "admin",
            },
        )

    @patch("xmlrpc.client.ServerProxy")
    def test_connect_success(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]
        mock_common.authenticate.return_value = 1

        client = OdooClient(odoo_creds)
        assert client.connect() is True
        assert client._uid == 1

    @patch("xmlrpc.client.ServerProxy")
    def test_connect_failure(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]
        mock_common.authenticate.return_value = False

        client = OdooClient(odoo_creds)
        assert client.connect() is False

    @patch("xmlrpc.client.ServerProxy")
    def test_create_record(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]
        mock_common.authenticate.return_value = 1
        mock_models.execute_kw.return_value = 42

        client = OdooClient(odoo_creds)
        client.connect()
        result = client.create_record("res.partner", {"name": "Test Co"})

        assert result.success is True
        assert result.record_id == "42"
        assert "name" in result.fields_written

    @patch("xmlrpc.client.ServerProxy")
    def test_update_record(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]
        mock_common.authenticate.return_value = 1
        mock_models.execute_kw.return_value = True

        client = OdooClient(odoo_creds)
        client.connect()
        result = client.update_record("res.partner", "42", {"name": "Updated Co"})

        assert result.success is True
        assert result.record_id == "42"

    @patch("xmlrpc.client.ServerProxy")
    def test_get_record(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]
        mock_common.authenticate.return_value = 1
        mock_models.execute_kw.return_value = [{"id": 42, "name": "Test Co"}]

        client = OdooClient(odoo_creds)
        client.connect()
        record = client.get_record("res.partner", "42")

        assert record is not None
        assert record["name"] == "Test Co"

    @patch("xmlrpc.client.ServerProxy")
    def test_execute_without_auth_raises(self, mock_proxy, odoo_creds):
        mock_common = MagicMock()
        mock_models = MagicMock()
        mock_proxy.side_effect = [mock_common, mock_models]

        client = OdooClient(odoo_creds)
        with pytest.raises(RuntimeError, match="Not authenticated"):
            client._execute("res.partner", "read", [[1]])


# ── Field Mapper ──────────────────────────────────────────────


class TestFieldMapper:
    """Test canonical → CRM field mapping."""

    @pytest.fixture
    def mapping_yaml(self, tmp_path):
        content = """
crm: odoo
version: 2.0
objects:
  company:
    odoo_model: res.partner
    mappings:
      company_name: name
      company_domain: website
      company_phone: phone
      enrichment_quality_score: data_quality_score
  contact:
    odoo_model: res.partner
    extra_domain:
      - [is_company, "=", false]
    mappings:
      contact_email: email
      contact_first_name: firstname
custom_fields:
  res.partner:
    - name: data_quality_score
      type: float
"""
        p = tmp_path / "odoo_mapping.yaml"
        p.write_text(content)
        return str(p)

    def test_to_crm_company(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        obj, payload = mapper.to_crm(
            "company",
            {
                "company_name": "Acme Corp",
                "company_domain": "acme.com",
                "company_phone": "+1-555-0100",
                "enrichment_quality_score": 0.85,
            },
        )
        assert obj == "res.partner"
        assert payload["name"] == "Acme Corp"
        assert payload["website"] == "acme.com"
        assert payload["data_quality_score"] == 0.85

    def test_to_crm_contact(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        obj, payload = mapper.to_crm(
            "contact",
            {"contact_email": "john@acme.com", "contact_first_name": "John"},
        )
        assert obj == "res.partner"
        assert payload["email"] == "john@acme.com"

    def test_to_crm_skips_empty(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        _, payload = mapper.to_crm(
            "company",
            {"company_name": "Acme", "company_domain": "", "company_phone": None},
        )
        assert "website" not in payload
        assert "phone" not in payload

    def test_to_crm_unknown_domain_raises(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        with pytest.raises(ValueError, match="No CRM mapping"):
            mapper.to_crm("unknown_domain", {})

    def test_extra_domain(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        extra = mapper.get_extra_domain("contact")
        assert extra is not None
        assert extra[0] == ["is_company", "=", False]

    def test_custom_fields(self, mapping_yaml):
        mapper = FieldMapper(mapping_yaml)
        custom = mapper.get_custom_fields("res.partner")
        assert len(custom) == 1
        assert custom[0]["name"] == "data_quality_score"
