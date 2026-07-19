"""
Apollo enrichment source adapter.

Provides company and contact enrichment via the Apollo.io API.
Maps Apollo response fields to L9 canonical field names.

L9 Architecture Note:
    Chassis-agnostic. Implements BaseSource contract.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import BaseSource, EnrichmentResult

logger = logging.getLogger(__name__)


class ApolloSource(BaseSource):
    """Apollo.io enrichment source for company and contact domains."""

    async def enrich(self, domain: str, payload: dict[str, Any]) -> EnrichmentResult:
        start = self._now_ms()

        if not self.config.enabled:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="source_disabled",
            )

        if not self.config.api_key:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="missing_api_key",
            )

        if domain == "company":
            return await self._enrich_company(payload, start)
        if domain == "contact":
            return await self._enrich_contact(payload, start)

        return EnrichmentResult(
            data={},
            quality_score=0.0,
            source_name=self.config.name,
            latency_ms=self._now_ms() - start,
            error="unsupported_domain",
        )

    async def _enrich_company(self, payload: dict[str, Any], start: int) -> EnrichmentResult:
        company_domain = payload.get("company_domain", "")
        if not company_domain:
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="missing_company_domain",
            )

        url = f"{self.config.api_endpoint}/organizations/enrich"
        headers = {
            "X-Api-Key": self.config.api_key or "",
            "Content-Type": "application/json",
        }
        body = {"domain": company_domain}

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("organization", {})
        except Exception as exc:
            logger.warning("Apollo company error: %s", exc)
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="network_error",
            )

        mapped = {
            "company_name": data.get("name"),
            "company_domain": data.get("primary_domain"),
            "company_industry": data.get("industry"),
            "company_description": data.get("short_description"),
            "employee_count": data.get("estimated_num_employees"),
            "annual_revenue": data.get("annual_revenue"),
            "company_location_city": data.get("city"),
            "company_location_state": data.get("state"),
            "company_location_country": data.get("country"),
            "company_linkedin_url": data.get("linkedin_url"),
            "company_phone": data.get("phone"),
            "company_founded_year": data.get("founded_year"),
        }

        non_null = sum(1 for v in mapped.values() if v not in (None, ""))
        quality = non_null / len(mapped) if mapped else 0.0

        return EnrichmentResult(
            data={k: v for k, v in mapped.items() if v not in (None, "")},
            quality_score=quality,
            source_name=self.config.name,
            latency_ms=self._now_ms() - start,
        )

    async def _enrich_contact(self, payload: dict[str, Any], start: int) -> EnrichmentResult:
        email = payload.get("contact_email", "")
        body: dict[str, Any] = {}
        if email:
            body["email"] = email
        else:
            first = payload.get("contact_first_name", "")
            last = payload.get("contact_last_name", "")
            domain = payload.get("company_domain", "")
            if last and domain:
                body["first_name"] = first
                body["last_name"] = last
                body["organization_domain"] = domain
            else:
                return EnrichmentResult(
                    data={},
                    quality_score=0.0,
                    source_name=self.config.name,
                    latency_ms=self._now_ms() - start,
                    error="missing_identifier",
                )

        url = f"{self.config.api_endpoint}/people/match"
        headers = {
            "X-Api-Key": self.config.api_key or "",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("person", {})
        except Exception as exc:
            logger.warning("Apollo contact error: %s", exc)
            return EnrichmentResult(
                data={},
                quality_score=0.0,
                source_name=self.config.name,
                latency_ms=self._now_ms() - start,
                error="network_error",
            )

        mapped = {
            "contact_first_name": data.get("first_name"),
            "contact_last_name": data.get("last_name"),
            "contact_title": data.get("title"),
            "contact_email": data.get("email"),
            "contact_phone": data.get("phone_numbers", [{}])[0].get("sanitized_number")
            if data.get("phone_numbers")
            else None,
            "contact_linkedin_url": data.get("linkedin_url"),
            "contact_company_name": data.get("organization", {}).get("name"),
            "contact_location_city": data.get("city"),
            "contact_location_state": data.get("state"),
            "contact_location_country": data.get("country"),
        }

        non_null = sum(1 for v in mapped.values() if v not in (None, ""))
        quality = non_null / len(mapped) if mapped else 0.0

        return EnrichmentResult(
            data={k: v for k, v in mapped.items() if v not in (None, "")},
            quality_score=quality,
            source_name=self.config.name,
            latency_ms=self._now_ms() - start,
        )
