"""Testes de detecção de anomalias de cost (Onda 4 PR #71).

Cobre o módulo `app.catalog.anomalies` (lógica de detecção pura) e o
endpoint `GET /api/v1/catalog/cost/anomalies` (gating de scope + audit).

Estratégia: monkeypatch das duas queries internas (_query_today_total e
_query_baseline_avg) para evitar Postgres. A lógica de threshold é o que
importa testar — o SQL fica pra smoke manual.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.core.database import audit_repo
from app.routes.catalog import router as catalog_router


# ═════════════════════════════════════════════════════════════════
# Parte 1 — Lógica pura de detect_anomalies
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_cost_queries(monkeypatch):
    """Patcheia as duas queries internas com valores controlados."""
    state = {"today": 0.0, "baseline": 0.0}

    async def fake_today(*args, **kwargs):
        return state["today"]

    async def fake_baseline(*args, **kwargs):
        return state["baseline"]

    monkeypatch.setattr("app.catalog.anomalies._query_today_total", fake_today)
    monkeypatch.setattr("app.catalog.anomalies._query_baseline_avg", fake_baseline)
    return state


@pytest.mark.asyncio
async def test_pico_detectado_quando_today_alto_e_baseline_acima_do_floor(fake_cost_queries):
    """Today = 5x baseline; baseline > $1 floor → pico_relativo."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 25.0
    fake_cost_queries["baseline"] = 5.0  # ratio = 5x → > PICO_MULTIPLIER (3.0)

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "pico_relativo" in types
    pico = next(a for a in result["anomalies"] if a["type"] == "pico_relativo")
    assert pico["ratio"] == 5.0
    assert pico["severity"] == "warning"


@pytest.mark.asyncio
async def test_pico_ignorado_quando_baseline_abaixo_do_floor(fake_cost_queries):
    """Baseline < $1: ratio é meaningless (qualquer valor parece pico).
    Não deve detectar."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 10.0
    fake_cost_queries["baseline"] = 0.50  # < PICO_MIN_BASELINE_USD (1.0)

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "pico_relativo" not in types


@pytest.mark.asyncio
async def test_pico_ignorado_quando_ratio_abaixo_do_threshold(fake_cost_queries):
    """Today=2x baseline (< 3x): variação normal, não é pico."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 10.0
    fake_cost_queries["baseline"] = 5.0  # ratio = 2x → < PICO_MULTIPLIER

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "pico_relativo" not in types


@pytest.mark.asyncio
async def test_limite_global_detectado(fake_cost_queries):
    """Today > $100 absoluto → limite_global, independente de baseline."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 150.0
    fake_cost_queries["baseline"] = 200.0  # baseline alto, então sem pico

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "limite_global" in types
    # Pico NÃO detectado (ratio < 3x)
    assert "pico_relativo" not in types


@pytest.mark.asyncio
async def test_limite_global_ignorado_quando_abaixo(fake_cost_queries):
    """Today exatamente no limite não dispara (usa > estrito)."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 100.0  # == LIMITE_GLOBAL_USD
    fake_cost_queries["baseline"] = 50.0

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "limite_global" not in types


@pytest.mark.asyncio
async def test_ambas_anomalias_simultaneas(fake_cost_queries):
    """Pico + limite global ao mesmo tempo: ambos registrados."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 500.0
    fake_cost_queries["baseline"] = 50.0  # ratio = 10x; today > $100

    result = await detect_anomalies()
    types = [a["type"] for a in result["anomalies"]]
    assert "pico_relativo" in types
    assert "limite_global" in types
    assert len(result["anomalies"]) == 2


@pytest.mark.asyncio
async def test_zero_anomalias_dia_normal(fake_cost_queries):
    """Caminho feliz: cost dentro do normal, sem alertas."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 30.0
    fake_cost_queries["baseline"] = 25.0  # ratio = 1.2x; today < $100

    result = await detect_anomalies()
    assert result["anomalies"] == []
    assert result["today_usd"] == 30.0
    assert result["baseline_avg_usd"] == 25.0


@pytest.mark.asyncio
async def test_shape_response_contém_thresholds(fake_cost_queries):
    """Response inclui os thresholds usados — auditoria/debug."""
    from app.catalog.anomalies import detect_anomalies

    fake_cost_queries["today"] = 0.0
    fake_cost_queries["baseline"] = 0.0

    result = await detect_anomalies()
    assert "thresholds" in result
    assert result["thresholds"]["pico_multiplier"] == 3.0
    assert result["thresholds"]["limite_global_usd"] == 100.0
    assert result["thresholds"]["baseline_window_days"] == 7


# ═════════════════════════════════════════════════════════════════
# Parte 2 — Endpoint GET /api/v1/catalog/cost/anomalies
# ═════════════════════════════════════════════════════════════════


def _client(user: dict) -> TestClient:
    app = FastAPI()
    app.include_router(catalog_router)
    app.dependency_overrides[require_user] = lambda: user
    return TestClient(app)


@pytest.fixture
def fake_anomalies_endpoint(monkeypatch):
    """Mock de detect_anomalies + audit para testar gating de scope."""
    calls = {"detect": [], "audit": []}

    async def fake_detect(*, consumer_user_id=None, consumer_department=None):
        calls["detect"].append({"user_id": consumer_user_id, "dept": consumer_department})
        # Default: sem anomalias. Tests podem mudar via monkeypatch.
        return {
            "checked_at": "2026-05-19T00:00:00Z",
            "today_usd": 5.0,
            "baseline_avg_usd": 2.0,
            "scope": {"consumer_user_id": consumer_user_id, "consumer_department": consumer_department},
            "anomalies": [],
            "thresholds": {"pico_multiplier": 3.0, "limite_global_usd": 100.0},
        }

    async def fake_audit_create(data):
        calls["audit"].append(dict(data))
        return data

    # detect_anomalies é importado dinamicamente dentro do endpoint
    monkeypatch.setattr("app.catalog.anomalies.detect_anomalies", fake_detect)
    monkeypatch.setattr(audit_repo, "create", fake_audit_create)
    return calls


class TestAnomaliesEndpoint:
    def test_200_root_scope_all(self, fake_anomalies_endpoint):
        c = _client({"id": "u-root", "role": "root"})
        r = c.get("/api/v1/catalog/cost/anomalies?scope=all")
        assert r.status_code == 200
        body = r.json()
        assert body["anomalies"] == []
        # scope=all → user_id None
        assert fake_anomalies_endpoint["detect"][-1]["user_id"] is None

    def test_403_scope_all_sem_root(self, fake_anomalies_endpoint):
        c = _client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/cost/anomalies?scope=all")
        assert r.status_code == 403

    def test_200_auto_root_vira_all(self, fake_anomalies_endpoint):
        c = _client({"id": "u-root", "role": "root"})
        r = c.get("/api/v1/catalog/cost/anomalies")  # default scope=auto
        assert r.status_code == 200
        assert fake_anomalies_endpoint["detect"][-1]["user_id"] is None

    def test_200_auto_comum_vira_mine(self, fake_anomalies_endpoint):
        c = _client({"id": "u1", "role": "comum"})
        r = c.get("/api/v1/catalog/cost/anomalies")  # default scope=auto
        assert r.status_code == 200
        assert fake_anomalies_endpoint["detect"][-1]["user_id"] == "u1"

    def test_filtro_department(self, fake_anomalies_endpoint):
        c = _client({"id": "u-root", "role": "root"})
        r = c.get("/api/v1/catalog/cost/anomalies?consumer_department=fiscal")
        assert r.status_code == 200
        assert fake_anomalies_endpoint["detect"][-1]["dept"] == "fiscal"

    def test_audit_gravado_quando_ha_anomalia(self, fake_anomalies_endpoint, monkeypatch):
        # Sobrescreve o detect para devolver 2 anomalias
        async def with_anomalies(*, consumer_user_id=None, consumer_department=None):
            return {
                "checked_at": "x", "today_usd": 200.0, "baseline_avg_usd": 50.0,
                "scope": {"consumer_user_id": consumer_user_id, "consumer_department": consumer_department},
                "anomalies": [
                    {"type": "pico_relativo", "severity": "warning", "message": "...", "value": 200, "threshold": 150, "ratio": 4.0},
                    {"type": "limite_global", "severity": "warning", "message": "...", "value": 200, "threshold": 100},
                ],
                "thresholds": {},
            }
        monkeypatch.setattr("app.catalog.anomalies.detect_anomalies", with_anomalies)

        c = _client({"id": "u-root", "role": "root"})
        r = c.get("/api/v1/catalog/cost/anomalies")
        assert r.status_code == 200
        # Audit registrado
        audits = fake_anomalies_endpoint["audit"]
        assert len(audits) == 1
        assert audits[0]["action"] == "cost_anomaly_detected"

    def test_audit_NAO_gravado_quando_sem_anomalia(self, fake_anomalies_endpoint):
        c = _client({"id": "u-root", "role": "root"})
        r = c.get("/api/v1/catalog/cost/anomalies")
        assert r.status_code == 200
        # Default fixture devolve anomalies=[] → sem audit
        assert len(fake_anomalies_endpoint["audit"]) == 0
