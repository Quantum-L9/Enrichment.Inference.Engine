"""
Tests for the read-only Odoo 19 JSON-2 CRM source adapter (app/services/odoo_crm_source.py).

Hermetic: all Odoo HTTP is mocked via respx. The adapter → CRMField conversion,
provenance propagation and sampling arithmetic execute real code.

Run: pytest tests/test_odoo_crm_source.py -v
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
import respx

from app.services.crm_field_scanner import CRMField, scan_crm_fields, scan_result_to_dict
from app.services.crm_source import CRMSourceError
from app.services.odoo_crm_source import OdooCRMSource, _is_populated, _sampleable

BASE_URL = "https://odoo.test"
API_KEY = "test-api-key"

PARTNER_FIELDS: dict[str, dict[str, Any]] = {
    "name": {"string": "Name", "type": "char"},
    "phone": {"string": "Phone", "type": "char"},
    "email": {"string": "Email", "type": "char"},
    "x_materials_handled": {"string": "Materials Handled", "type": "char"},
    "image_1920": {"string": "Image", "type": "binary"},
    "child_ids": {"string": "Contacts", "type": "one2many", "relation": "res.partner"},
    "active": {"string": "Active", "type": "boolean"},
}

LEAD_FIELDS: dict[str, dict[str, Any]] = {
    "name": {"string": "Opportunity", "type": "char"},
    "phone": {"string": "Phone", "type": "char"},
    "email_from": {"string": "Email", "type": "char"},
    "partner_id": {"string": "Customer", "type": "many2one", "relation": "res.partner"},
    "tag_ids": {"string": "Tags", "type": "many2many", "relation": "crm.tag"},
    "x_plastic_type": {"string": "Plastic Type", "type": "selection"},
}

PARTNER_ROWS: list[dict[str, Any]] = [
    {"id": 1, "name": "Acme", "phone": "+1 555 0100", "email": "a@acme.test", "active": True},
    {"id": 2, "name": "Beta", "phone": False, "email": "b@beta.test", "active": False},
    {"id": 3, "name": "Gamma", "phone": False, "email": False, "active": True},
]

LEAD_ROWS: list[dict[str, Any]] = [
    {
        "id": 10,
        "name": "Deal 1",
        "email_from": "x@x.test",
        "partner_id": [1, "Acme"],
        "tag_ids": [1, 2],
    },
    {"id": 11, "name": "Deal 2", "email_from": "y@y.test", "partner_id": False, "tag_ids": []},
    {
        "id": 12,
        "name": "Deal 3",
        "email_from": "z@z.test",
        "partner_id": [1, "Acme"],
        "tag_ids": [],
    },
    {"id": 13, "name": "Deal 4", "email_from": False, "partner_id": False, "tag_ids": []},
    {"id": 14, "name": "Deal 5", "email_from": False, "partner_id": [2, "Beta"], "tag_ids": [3]},
]


def _url(model: str, method: str) -> str:
    return f"{BASE_URL}/json/2/{model}/{method}"


def _mock_odoo(
    router: respx.MockRouter,
    *,
    partner_fields: dict[str, Any] | None = None,
    lead_fields: dict[str, Any] | None = None,
    partner_rows: list[dict[str, Any]] | None = None,
    lead_rows: list[dict[str, Any]] | None = None,
) -> dict[str, respx.Route]:
    pf = PARTNER_FIELDS if partner_fields is None else partner_fields
    lf = LEAD_FIELDS if lead_fields is None else lead_fields
    pr = PARTNER_ROWS if partner_rows is None else partner_rows
    lr = LEAD_ROWS if lead_rows is None else lead_rows
    return {
        "partner_fields": router.post(_url("res.partner", "fields_get")).mock(
            return_value=httpx.Response(200, json=pf)
        ),
        "partner_rows": router.post(_url("res.partner", "search_read")).mock(
            return_value=httpx.Response(200, json=pr)
        ),
        "lead_fields": router.post(_url("crm.lead", "fields_get")).mock(
            return_value=httpx.Response(200, json=lf)
        ),
        "lead_rows": router.post(_url("crm.lead", "search_read")).mock(
            return_value=httpx.Response(200, json=lr)
        ),
    }


def _source(database: str = "test-db") -> OdooCRMSource:
    return OdooCRMSource(base_url=BASE_URL, api_key=API_KEY, database=database)


def _by(fields: list[CRMField], resource: str, name: str) -> CRMField:
    matches = [f for f in fields if f.source_resource == resource and f.name == name]
    assert len(matches) == 1, f"expected exactly one {resource}.{name}, got {len(matches)}"
    return matches[0]


# ── Adapter conversion ───────────────────────────────────────────────────────


class TestAdapterConversion:
    @respx.mock
    async def test_reads_contacts_fields(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        partner = {f.name for f in fields if f.source_resource == "res.partner"}
        assert partner == set(PARTNER_FIELDS)

    @respx.mock
    async def test_reads_crm_fields(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        leads = {f.name for f in fields if f.source_resource == "crm.lead"}
        assert leads == set(LEAD_FIELDS)

    @respx.mock
    async def test_reads_both_resources(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source().fields()
        for route in routes.values():
            assert route.call_count == 1

    @respx.mock
    async def test_preserves_resource_identity(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        assert all(f.source_system == "odoo" for f in fields)
        assert {f.source_resource for f in fields} == {"res.partner", "crm.lead"}

    @respx.mock
    async def test_preserves_duplicate_names_across_resources(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        phones = [f for f in fields if f.name == "phone"]
        assert len(phones) == 2
        assert {f.source_resource for f in phones} == {"res.partner", "crm.lead"}
        assert all(f.name == "phone" for f in phones)  # not renamed

    @respx.mock
    async def test_discovers_custom_fields(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        custom = _by(fields, "crm.lead", "x_plastic_type")
        assert custom.field_type == "selection"
        assert custom.source_system == "odoo"
        assert _by(fields, "res.partner", "x_materials_handled").field_type == "char"

    @respx.mock
    async def test_installed_addon_extension_discovered_without_code_change(self) -> None:
        extended = {**LEAD_FIELDS, "x_studio_recycler_grade": {"string": "Grade", "type": "char"}}
        _mock_odoo(respx.mock, lead_fields=extended)
        fields = await _source().fields()
        assert _by(fields, "crm.lead", "x_studio_recycler_grade").field_type == "char"

    @respx.mock
    async def test_uses_technical_name_not_label(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        names = {f.name for f in fields if f.source_resource == "crm.lead"}
        assert "email_from" in names
        assert "Email" not in names

    @respx.mock
    async def test_computes_sample_values(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        email = _by(fields, "res.partner", "email")
        assert email.sample_values == ["a@acme.test", "b@beta.test"]

    @respx.mock
    async def test_sample_values_deduplicated_and_capped(self) -> None:
        rows = [{"id": i, "name": f"n{i % 3}"} for i in range(20)]
        _mock_odoo(respx.mock, lead_rows=rows)
        fields = await _source().fields()
        name = _by(fields, "crm.lead", "name")
        assert name.sample_values == ["n0", "n1", "n2"]
        rows = [{"id": i, "name": f"n{i}"} for i in range(20)]
        respx.mock.post(_url("crm.lead", "search_read")).mock(
            return_value=httpx.Response(200, json=rows)
        )
        fields = await _source().fields()
        assert len(_by(fields, "crm.lead", "name").sample_values) == 5

    @respx.mock
    async def test_computes_fill_rate(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        assert _by(fields, "crm.lead", "email_from").fill_rate == 0.6  # 3 of 5
        assert _by(fields, "res.partner", "phone").fill_rate == 0.3333  # 1 of 3, 4 dp

    @respx.mock
    async def test_empty_model_has_none_fill_rate(self) -> None:
        _mock_odoo(respx.mock, lead_rows=[])
        fields = await _source().fields()
        lead_fields = [f for f in fields if f.source_resource == "crm.lead"]
        assert {f.name for f in lead_fields} == set(LEAD_FIELDS)
        assert all(f.sample_values == [] for f in lead_fields)
        assert all(f.fill_rate is None for f in lead_fields)

    @respx.mock
    async def test_boolean_false_counts_as_populated(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        active = _by(fields, "res.partner", "active")
        assert active.fill_rate == 1.0
        assert active.sample_values == [True, False]

    @respx.mock
    async def test_excludes_binary_and_one2many_from_sampling(self) -> None:
        routes = _mock_odoo(respx.mock)
        fields = await _source().fields()
        requested = routes["partner_rows"].calls.last.request
        body = httpx.Response(200, content=requested.content).json()
        assert "image_1920" not in body["fields"]
        assert "child_ids" not in body["fields"]
        # Still discovered as schema, just not sampled.
        image = _by(fields, "res.partner", "image_1920")
        assert image.field_type == "binary"
        assert image.sample_values == []
        assert image.fill_rate is None

    @respx.mock
    async def test_does_not_follow_relations(self) -> None:
        routes = _mock_odoo(respx.mock)
        fields = await _source().fields()
        partner_id = _by(fields, "crm.lead", "partner_id")
        assert partner_id.sample_values == [[1, "Acme"], [2, "Beta"]]
        assert _by(fields, "crm.lead", "tag_ids").sample_values == [[1, 2], [3]]
        assert sum(r.call_count for r in routes.values()) == 4

    @respx.mock
    async def test_output_sorted_deterministically(self) -> None:
        _mock_odoo(respx.mock)
        fields = await _source().fields()
        keys = [(f.source_resource, f.name) for f in fields]
        assert keys == sorted(keys)


# ── HTTP contract ────────────────────────────────────────────────────────────


class TestHttpContract:
    @respx.mock
    async def test_uses_json2_endpoint_for_both_models_once_each(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source().fields()
        assert routes["partner_fields"].call_count == 1
        assert routes["partner_rows"].call_count == 1
        assert routes["lead_fields"].call_count == 1
        assert routes["lead_rows"].call_count == 1
        assert len(respx.mock.calls) == 4

    @respx.mock
    async def test_sends_bearer_api_key_and_headers(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source().fields()
        request = routes["partner_fields"].calls.last.request
        assert request.headers["authorization"] == "bearer test-api-key"
        assert request.headers["content-type"].startswith("application/json")
        assert request.headers["user-agent"].startswith("enrichment-inference-engine/")

    @respx.mock
    async def test_sends_database_header_when_configured(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source(database="test-db").fields()
        assert routes["lead_rows"].calls.last.request.headers["x-odoo-database"] == "test-db"

    @respx.mock
    async def test_omits_database_header_when_unconfigured(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source(database="").fields()
        assert "x-odoo-database" not in routes["lead_rows"].calls.last.request.headers

    @respx.mock
    async def test_fields_get_and_search_read_payloads(self) -> None:
        routes = _mock_odoo(respx.mock)
        await _source().fields(sample_limit=7)
        meta_body = httpx.Response(
            200, content=routes["lead_fields"].calls.last.request.content
        ).json()
        assert "attributes" in meta_body and "type" in meta_body["attributes"]
        rows_body = httpx.Response(
            200, content=routes["lead_rows"].calls.last.request.content
        ).json()
        assert rows_body["domain"] == []
        assert rows_body["limit"] == 7
        assert set(rows_body["fields"]) == {
            "name",
            "phone",
            "email_from",
            "partner_id",
            "tag_ids",
            "x_plastic_type",
        }

    @respx.mock
    async def test_base_url_trailing_slash_normalized(self) -> None:
        routes = _mock_odoo(respx.mock)
        await OdooCRMSource(base_url=BASE_URL + "/", api_key=API_KEY).fields()
        assert routes["partner_fields"].call_count == 1

    @respx.mock
    async def test_timeout_is_source_error(self) -> None:
        _mock_odoo(respx.mock)
        respx.mock.post(_url("res.partner", "fields_get")).mock(
            side_effect=httpx.ReadTimeout("slow")
        )
        with pytest.raises(CRMSourceError, match="timed out.*res.partner.*fields_get"):
            await _source().fields()

    @respx.mock
    async def test_connection_failure_is_source_error(self) -> None:
        _mock_odoo(respx.mock)
        respx.mock.post(_url("res.partner", "fields_get")).mock(
            side_effect=httpx.ConnectError("refused")
        )
        with pytest.raises(CRMSourceError, match="ConnectError"):
            await _source().fields()

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    @respx.mock
    async def test_non_2xx_is_source_error(self, status: int) -> None:
        _mock_odoo(respx.mock)
        respx.mock.post(_url("crm.lead", "fields_get")).mock(
            return_value=httpx.Response(
                status, json={"name": "odoo.exceptions.AccessError", "message": "secret-body"}
            )
        )
        with pytest.raises(CRMSourceError) as exc_info:
            await _source().fields()
        message = str(exc_info.value)
        assert f"status={status}" in message
        assert "crm.lead" in message and "fields_get" in message
        assert "secret-body" not in message
        assert API_KEY not in message

    @respx.mock
    async def test_invalid_json_is_source_error(self) -> None:
        _mock_odoo(respx.mock)
        respx.mock.post(_url("res.partner", "search_read")).mock(
            return_value=httpx.Response(200, content=b"<html>not json</html>")
        )
        with pytest.raises(CRMSourceError, match="invalid JSON.*res.partner.*search_read"):
            await _source().fields()

    @respx.mock
    async def test_unexpected_shape_is_source_error(self) -> None:
        _mock_odoo(respx.mock)
        respx.mock.post(_url("crm.lead", "fields_get")).mock(
            return_value=httpx.Response(200, json=["not", "a", "dict"])
        )
        with pytest.raises(CRMSourceError, match="unexpected shape.*crm.lead.*fields_get"):
            await _source().fields()

    @respx.mock
    async def test_contacts_ok_but_crm_inaccessible_fails_whole_discovery(self) -> None:
        routes = _mock_odoo(respx.mock)
        respx.mock.post(_url("crm.lead", "fields_get")).mock(
            return_value=httpx.Response(403, json={"name": "odoo.exceptions.AccessError"})
        )
        with pytest.raises(CRMSourceError, match="crm.lead"):
            await _source().fields()
        assert routes["partner_fields"].call_count == 1  # Contacts succeeded first

    async def test_missing_config_is_source_error(self) -> None:
        with pytest.raises(CRMSourceError, match="ODOO_URL and ODOO_API_KEY"):
            OdooCRMSource(base_url="", api_key=API_KEY)
        with pytest.raises(CRMSourceError, match="ODOO_URL and ODOO_API_KEY"):
            OdooCRMSource(base_url=BASE_URL, api_key="")

    @pytest.mark.parametrize("limit", [0, 101, 1000])
    async def test_sample_limit_bound_enforced_in_code(self, limit: int) -> None:
        with pytest.raises(CRMSourceError, match="sample_limit"):
            await _source().fields(sample_limit=limit)

    async def test_arbitrary_model_or_method_refused(self) -> None:
        source = _source()
        async with httpx.AsyncClient() as client:
            with pytest.raises(CRMSourceError, match="refused"):
                await source._call(client, "res.users", "search_read", {})
            with pytest.raises(CRMSourceError, match="refused"):
                await source._call(client, "res.partner", "unlink", {})


# ── Helpers ──────────────────────────────────────────────────────────────────


def test_sampleable_allowlist() -> None:
    assert _sampleable({"type": "char"})
    assert _sampleable({"type": "many2one"})
    assert not _sampleable({"type": "binary"})
    assert not _sampleable({"type": "one2many"})
    assert not _sampleable({"type": "reference"})
    assert not _sampleable({})


def test_is_populated_semantics() -> None:
    for empty in (None, False, "", [], {}):
        assert not _is_populated(empty, "char")
    assert _is_populated(False, "boolean")
    assert _is_populated(True, "boolean")
    assert not _is_populated(None, "boolean")
    assert _is_populated(0, "integer")
    assert _is_populated([1, "Acme"], "many2one")


# ── Provenance through the existing scanner ──────────────────────────────────


@respx.mock
async def test_live_fields_flow_through_existing_scanner_with_provenance() -> None:
    _mock_odoo(respx.mock)
    domain_spec = {
        "domain": {"id": "plasticos", "version": "1.0.0"},
        "ontology": {
            "nodes": [
                {
                    "label": "Partner",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {"type": "string"},
                        "materials_handled": {"type": "string"},
                    },
                }
            ]
        },
    }
    fields = await _source().fields()
    result = scan_result_to_dict(scan_crm_fields(fields, domain_spec))

    phone_matches = [m for m in result["matched"] if m["crm_field"] == "phone"]
    assert {m["source_resource"] for m in phone_matches} == {"res.partner", "crm.lead"}
    assert all(m["source_system"] == "odoo" for m in phone_matches)
    materials = [m for m in result["matched"] if m["crm_field"] == "x_materials_handled"]
    assert materials[0]["source_resource"] == "res.partner"
    plastic = [u for u in result["unmapped"] if u["crm_field"] == "x_plastic_type"]
    assert plastic == [
        {"crm_field": "x_plastic_type", "source_system": "odoo", "source_resource": "crm.lead"}
    ]


# ── Opt-in real Odoo 19 integration test ─────────────────────────────────────

_REAL_URL = os.environ.get("EIE_TEST_ODOO_URL", "")
_REAL_KEY = os.environ.get("EIE_TEST_ODOO_API_KEY", "")


@pytest.mark.integration
@pytest.mark.skipif(
    not (_REAL_URL and _REAL_KEY),
    reason="EIE_TEST_ODOO_URL / EIE_TEST_ODOO_API_KEY not set — real Odoo test is opt-in",
)
async def test_real_odoo_contacts_and_crm_discovery() -> None:
    """Read-only against a real Odoo 19 database. Zero writes; universal fields only."""
    source = OdooCRMSource(
        base_url=_REAL_URL,
        api_key=_REAL_KEY,
        database=os.environ.get("EIE_TEST_ODOO_DB", ""),
    )
    fields = await source.fields(sample_limit=5)
    assert _by(fields, "res.partner", "name").source_system == "odoo"
    assert _by(fields, "crm.lead", "name").source_system == "odoo"
    assert {f.source_resource for f in fields} == {"res.partner", "crm.lead"}
