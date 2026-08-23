"""
SOQL Literal and Identifier Contract
Source: app/services/crm/salesforce_client.py — _soql_escape / _soql_literal / query_records.
Markers: unit

Salesforce SOQL reserves backslash and single-quote inside string literals.
`_soql_escape` must escape the backslash FIRST: escaping quotes first leaves
the quote unescaped once the backslash is expanded, which is precisely the
bypass the escaping exists to prevent. That ordering is a contract with the
SOQL specification, not an implementation detail, which is why it is asserted
here rather than in a client-specific unit test.

Before this file, none of it had ever executed. salesforce_client.py sat at
20.42% line coverage with the bodies of _soql_escape (57-58) and _soql_literal
(70-76) entirely uncovered, and no test called query_records at all — so the
guards the module docstring advertises as its SEC-SQL remediation were
unverified.

Every app import is deferred into a test body. The L9 Constitution Gate
collects tests/contracts/ in a lean environment without the provider SDKs, and
a module-level app import here breaks collection (see
test_prompt_envelope_contract.py, which documents the same constraint).
"""

from __future__ import annotations

from typing import Any

import pytest


class TestSoqlEscape:
    """The escape ordering is the whole security property."""

    def test_wraps_and_escapes_single_quote(self) -> None:
        from app.services.crm.salesforce_client import _soql_escape

        assert _soql_escape("O'Brien") == r"'O\'Brien'"

    def test_backslash_is_escaped_before_quote(self) -> None:
        r"""Input `\'` must not yield a literal backslash plus a live quote.

        Quote-first ordering would produce `'\\''` — backslash escaping its
        own backslash, quote left unescaped, string terminated early. That is
        the injection this ordering prevents, and it is the case the docstring
        at salesforce_client.py:48-53 calls out by name.
        """
        from app.services.crm.salesforce_client import _soql_escape

        assert _soql_escape("\\'") == r"'\\\''"

    def test_plain_value_is_only_quoted(self) -> None:
        from app.services.crm.salesforce_client import _soql_escape

        assert _soql_escape("Acme") == "'Acme'"


class TestSoqlLiteral:
    """Type mapping, including the bool-before-int ordering."""

    def test_bool_is_checked_before_int(self) -> None:
        """bool subclasses int; a plain isinstance(int) check emits 1/0."""
        from app.services.crm.salesforce_client import _soql_literal

        assert _soql_literal(True) == "true"
        assert _soql_literal(False) == "false"

    def test_numerics_are_bare(self) -> None:
        from app.services.crm.salesforce_client import _soql_literal

        assert _soql_literal(3) == "3"
        assert _soql_literal(1.5) == "1.5"

    def test_none_is_null(self) -> None:
        from app.services.crm.salesforce_client import _soql_literal

        assert _soql_literal(None) == "null"

    def test_string_is_escaped(self) -> None:
        from app.services.crm.salesforce_client import _soql_literal

        assert _soql_literal("O'Brien") == r"'O\'Brien'"

    def test_other_types_are_coerced_and_escaped(self) -> None:
        """Unknown types go through str() and are then escaped.

        repr() renders a list containing a single-quote using double quotes,
        so the embedded quote survives str() and must be escaped by
        _soql_escape rather than terminating the SOQL literal.
        """
        from app.services.crm.salesforce_client import _soql_literal

        assert _soql_literal(["a'b"]) == "'[\"a\\'b\"]'"


def _client() -> Any:
    from app.services.crm.base import CRMCredentials, CRMType
    from app.services.crm.salesforce_client import SalesforceClient

    client = SalesforceClient(CRMCredentials(crm_type=CRMType.SALESFORCE, credentials={}))
    client._instance_url = "https://example.my.salesforce.com"
    return client


class TestQueryRecordsGuards:
    """All three interpolated slots must reject non-identifier input.

    The autouse fixture makes these hermetic and strengthens them: a guard
    must reject BEFORE any request is issued. Without it, a regression here
    silently performs real network I/O with the injected value — which is
    exactly what happens against the pre-fix code, where the unvalidated
    field list reaches httpx.get and the test fails on a 502 rather than on
    the missing guard.
    """

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.crm import salesforce_client as mod

        def _forbidden(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("guard did not reject before issuing a request")

        monkeypatch.setattr(mod.httpx, "get", _forbidden)

    def test_rejects_bad_object_type(self) -> None:
        client = _client()
        with pytest.raises(ValueError, match="Invalid SOQL object type"):
            client.query_records("Account WHERE Id != null OR '1'='1", {})

    def test_rejects_bad_filter_key(self) -> None:
        client = _client()
        with pytest.raises(ValueError, match="Invalid SOQL field name"):
            client.query_records("Account", {"Name = 'x' OR Id != null--": "v"})

    def test_rejects_bad_field_name(self) -> None:
        """The slot that had NO guard before this change.

        `fields` was joined straight into the SELECT list while object_type and
        filter keys were both validated — an asymmetry in the very defence the
        module docstring claims. Latent only because the sole caller
        (writeback.py:98) passes the literal ["id"].
        """
        client = _client()
        with pytest.raises(ValueError, match="Invalid SOQL field name"):
            client.query_records("Account", {}, fields=["Id FROM Account WHERE Name != null--"])

    def test_rejects_wildcard_field(self) -> None:
        """`*` is not an identifier; callers name their fields."""
        client = _client()
        with pytest.raises(ValueError, match="Invalid SOQL field name"):
            client.query_records("Account", {}, fields=["*"])


def _capture_query(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch httpx.get and return a dict that receives the emitted SOQL.

    Shared by both cases below: duplicating the stub response class per test
    is what SonarCloud flags as duplicated new code, and the ellipsis bodies
    it needs read as no-op statements to CodeQL.
    """
    captured: dict[str, Any] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"records": []}

    def _fake_get(url: str, **kwargs: Any) -> _Resp:
        captured["q"] = kwargs["params"]["q"]
        return _Resp()

    from app.services.crm import salesforce_client as mod

    monkeypatch.setattr(mod.httpx, "get", _fake_get)
    return captured


class TestQueryRecordsEmitsSafeSoql:
    def test_hostile_value_is_escaped_into_the_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A quote in a filter VALUE must be escaped, not terminate the literal."""
        captured = _capture_query(monkeypatch)
        _client().query_records("Account", {"Name": "O'Brien"}, fields=["Id", "Name"])

        assert captured["q"] == r"SELECT Id, Name FROM Account WHERE Name = 'O\'Brien'"

    def test_no_filters_uses_tautology_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _capture_query(monkeypatch)
        _client().query_records("Account", {})

        assert captured["q"] == "SELECT Id, Name FROM Account WHERE Id != null"
