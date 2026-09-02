"""
app/api/v1/chassis_endpoint.py
Supplemental Gate runtime routes for ENRICH.

`/v1/execute` is owned by the SDK-created node runtime. This module keeps
app-specific routes that are adjacent to transport, but not part of the SDK.

Retired 2026-09-02 (seam audit, finding EIE-SIDE-DOOR-04): `POST /v1/outcomes`.
It was a peer-facing HTTP ingress ("called by ROUTE/SCORE") that relayed match
outcomes into Cognitive.Engine.Graphs from EIE, i.e. node-to-node traffic that
entered EIE without Gate and left EIE as an EIE-authored packet. Outcome
feedback is a CEG-owned Gate action (`outcomes`); a producer sends it to Gate
directly, and Gate dispatches it to CEG. EIE is not a relay.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["chassis"])

# No routes are mounted. The router is kept so the active transport bundle
# (docs/ARCHITECTURE.md, tests/compliance/test_architecture.py) has one stable
# place for future transport-adjacent, non-peer routes.
