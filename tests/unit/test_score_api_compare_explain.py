"""/score/compare and /score/explain against the real app, providers overridden.

/score/compare named its two records with `before_score_id` / `after_score_id`
but read both with `score_store.get_score(entity_id, entity_id)` — the second
argument is the domain slot — so the ids it was given never selected anything.
It also called `ScoreExplainer.compare_scores(before, after, profile)`, and
/score/explain called `ScoreExplainer.explain(record, profile)`; neither method
takes a profile, so both raised TypeError once a store answered.

The five /score providers are not wired in production (no ProfileStore,
ScoreStore, DecayScoreStore or EntityDataProvider implementation exists), so
these tests override them on `app.main:app` with the in-memory doubles below.
The doubles exist only here and are never wired into the running app.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.score.score_api import (
    AFTER_SCORE_NOT_FOUND,
    BEFORE_SCORE_NOT_FOUND,
    get_explainer,
    get_profile_store,
    get_score_store,
)
from app.score.score_engine import ScoreStore
from app.score.score_explainer import ScoreExplainer
from app.score.score_models import (
    DimensionScore,
    MissingField,
    ScoreDimension,
    ScoreRecord,
    ScoreTier,
    ScoringProfile,
)

pytestmark = pytest.mark.unit

# sha256("pass"), the key tests/test_api.py authenticates with.
_KEY_HASH = "d74ff0ee8da3b9806b18c877dbf29bbde50b5bd8e4dad7a3a725000feb82e8f1"
AUTH = {"X-API-Key": "pass"}
ENTITY = "acct-1"
DOMAIN = "plastics"


class _ScoreStoreDouble:
    """In-memory ScoreStore: by score id, and latest per (entity, domain)."""

    def __init__(self, records: list[ScoreRecord]) -> None:
        self._by_id = {r.score_id: r for r in records}

    def save_score(self, record: ScoreRecord) -> None:
        self._by_id[record.score_id] = record

    def get_score(self, entity_id: str, domain: str) -> ScoreRecord | None:
        matches = [
            r for r in self._by_id.values() if r.entity_id == entity_id and r.domain == domain
        ]
        return max(matches, key=lambda r: r.scored_at) if matches else None

    def get_score_by_id(self, score_id: str) -> ScoreRecord | None:
        return self._by_id.get(score_id)

    def get_scores_batch(self, entity_ids: list[str], domain: str) -> list[ScoreRecord]:
        return [r for e in entity_ids if (r := self.get_score(e, domain)) is not None]

    def list_scores(self, domain: str, tier: ScoreTier | None, limit: int) -> list[ScoreRecord]:
        rows = [r for r in self._by_id.values() if r.domain == domain]
        return [r for r in rows if tier is None or r.tier == tier][:limit]


class _ProfileStoreDouble:
    def __init__(self, profile: ScoringProfile) -> None:
        self._profiles = {profile.profile_id: profile}

    def get_profile(self, profile_id: str) -> ScoringProfile | None:
        return self._profiles.get(profile_id)

    def save_profile(self, profile: ScoringProfile) -> None:
        self._profiles[profile.profile_id] = profile

    def list_profiles(self, domain: str) -> list[ScoringProfile]:
        return [p for p in self._profiles.values() if p.domain == domain]


def _record(
    score_id: str,
    composite: float,
    tier: ScoreTier,
    missing: list[str],
    age_days: int,
    entity_id: str = ENTITY,
) -> ScoreRecord:
    missing_fields = [
        MissingField(field_name=name, dimension=ScoreDimension.FIT, impact_estimate=0.2)
        for name in missing
    ]
    return ScoreRecord(
        score_id=score_id,
        entity_id=entity_id,
        domain=DOMAIN,
        composite_score=composite,
        composite_confidence=0.8,
        tier=tier,
        dimension_scores={
            ScoreDimension.FIT: DimensionScore(
                dimension=ScoreDimension.FIT,
                score=composite,
                confidence=0.8,
                weight=1.0,
                missing_fields=missing_fields,
            )
        },
        missing_fields=missing_fields,
        scored_at=datetime.now(UTC) - timedelta(days=age_days),
    )


BEFORE = _record("score-before", 0.30, ScoreTier.COLD, ["revenue", "employees"], age_days=20)
AFTER = _record("score-after", 0.70, ScoreTier.WARM, ["employees"], age_days=10)
# The newest record for the entity: what `get_score(entity, domain)` answers.
# Neither side of the comparison below may be read from it.
LATEST = _record("score-latest", 0.95, ScoreTier.HOT, [], age_days=0)
OTHER_ENTITY = _record("score-other", 0.50, ScoreTier.COOL, [], age_days=5, entity_id="acct-2")
PROFILE = ScoringProfile(name="plastics-icp", domain=DOMAIN)


@pytest.fixture(scope="module", autouse=True)
def _module_env() -> Iterator[None]:
    with patch.dict(os.environ, {"API_KEY_HASH": _KEY_HASH, "L9_ENVIRONMENT": "test"}):
        get_settings.cache_clear()
        try:
            yield
        finally:
            get_settings.cache_clear()


@pytest.fixture
def client() -> Iterator[TestClient]:
    store = _ScoreStoreDouble([BEFORE, AFTER, LATEST, OTHER_ENTITY])
    profiles = _ProfileStoreDouble(PROFILE)
    app.dependency_overrides[get_score_store] = lambda: store
    app.dependency_overrides[get_profile_store] = lambda: profiles
    app.dependency_overrides[get_explainer] = ScoreExplainer
    try:
        yield TestClient(app)
    finally:
        for provider in (get_score_store, get_profile_store, get_explainer):
            app.dependency_overrides.pop(provider, None)


def _compare(client: TestClient, before_id: str, after_id: str):
    return client.post(
        "/score/compare",
        json={
            "entity_id": ENTITY,
            "before_score_id": before_id,
            "after_score_id": after_id,
            "scoring_profile_id": PROFILE.profile_id,
        },
        headers=AUTH,
    )


def test_double_satisfies_the_score_store_protocol() -> None:
    assert isinstance(_ScoreStoreDouble([]), ScoreStore)


def test_compare_reads_each_record_by_its_own_score_id(client: TestClient) -> None:
    r = _compare(client, BEFORE.score_id, AFTER.score_id)

    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["entity_id"] == ENTITY
    assert payload["composite_before"] == 0.30
    assert payload["composite_after"] == 0.70
    assert payload["composite_delta"] == 0.40
    assert payload["tier_before"] == "cold"
    assert payload["tier_after"] == "warm"
    assert payload["tier_changed"] is True
    assert payload["fields_resolved"] == ["revenue"]
    assert payload["missing_before"] == 2
    assert payload["missing_after"] == 1
    assert payload["dimension_deltas"]["fit"] == {"before": 0.3, "after": 0.7, "delta": 0.4}


def test_compare_order_follows_the_ids_not_recency(client: TestClient) -> None:
    r = _compare(client, AFTER.score_id, BEFORE.score_id)

    assert r.status_code == 200, r.text
    payload = r.json()["payload"]
    assert payload["composite_before"] == 0.70
    assert payload["composite_after"] == 0.30
    assert payload["composite_delta"] == -0.40


def test_compare_unknown_before_id_is_404(client: TestClient) -> None:
    r = _compare(client, "no-such-score", AFTER.score_id)
    assert r.status_code == 404
    assert r.json()["detail"] == BEFORE_SCORE_NOT_FOUND


def test_compare_refuses_a_score_of_another_entity(client: TestClient) -> None:
    r = _compare(client, BEFORE.score_id, OTHER_ENTITY.score_id)
    assert r.status_code == 404
    assert r.json()["detail"] == AFTER_SCORE_NOT_FOUND


def test_explain_returns_the_explanation(client: TestClient) -> None:
    r = client.post(
        "/score/explain",
        json={"entity_id": ENTITY, "scoring_profile_id": PROFILE.profile_id, "domain": DOMAIN},
        headers=AUTH,
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operation"] == "explain_score"
    # `get_score(entity, domain)` answers the newest record.
    assert body["payload"]["entity_id"] == ENTITY
    assert body["payload"]["composite_score"] == pytest.approx(0.95, abs=1e-4)
