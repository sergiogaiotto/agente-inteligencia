"""Testes do endpoint POST /api/v1/knowledge-sources/{ks_id}/query.

Bug histórico (corrigido em PR #222): o caller chamava `retriever.retrieve()`
— método inexistente — em vez de `retriever.search()`. Sempre dava 500 com
`AttributeError: 'Retriever' object has no attribute 'retrieve'`.

O endpoint nasceu sem cobertura, então o bug viveu silencioso. Quando o
operador abriu o painel "Inspecionar Base > Test Query" e clicou em "Buscar",
o backend explodia.

Estes testes garantem:
1. Não-regressão do bug central (.retrieve vs .search)
2. Validações 400/404 (query vazia, KB inexistente)
3. Clamp do top_n (1..20)
4. Escopo restrito à própria KB (allowed_source_ids)
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── Fakes ──────────────────────────────────────────────────────


@dataclass
class _FakeEvidenceResult:
    """Forma mínima esperada pelo endpoint ao montar a response."""
    evidence_id: str = "ev-1"
    snippet_text: str = "trecho de evidência"
    relevance_score: float | None = 0.42
    source_name: str | None = "FAQ Claro"
    confidentiality: str | None = "public"


class _FakeRetriever:
    """Captura chamadas a `.search` e devolve resultado controlado.

    NÃO implementa `.retrieve` de propósito — se o endpoint regredisse para
    o nome antigo, AttributeError voltaria e os testes pegariam imediatamente.
    """
    last_kwargs: dict | None = None
    next_results: list = []

    async def search(self, **kwargs):
        _FakeRetriever.last_kwargs = kwargs
        return list(_FakeRetriever.next_results)


# ─── Fixture ────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    """FastAPI com router do dashboard + mocks de KB e Retriever."""
    async def fake_find_by_id(ks_id: str):
        # "ghost" reproduz KB inexistente para validar o 404
        return None if ks_id == "ghost" else {"id": ks_id, "name": "Stub"}

    monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_find_by_id)
    monkeypatch.setattr("app.evidence.runtime.Retriever", _FakeRetriever)
    _FakeRetriever.last_kwargs = None
    _FakeRetriever.next_results = [_FakeEvidenceResult()]

    from app.routes.dashboard import router as dashboard_router
    app = FastAPI()
    app.include_router(dashboard_router)
    return TestClient(app)


# ─── Testes ─────────────────────────────────────────────────────


class TestKBQueryEndpoint:
    KS = "46d4eeff-c461-4445-a7d3-57007716325a"

    def test_calls_retriever_search_not_retrieve(self, client):
        """Regressão central: endpoint chama .search() (e não .retrieve())."""
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "faturamento", "top_n": 5},
        )
        # Se algum dia voltar a chamar .retrieve(), _FakeRetriever não tem
        # esse método → AttributeError → 500. Este teste falha no primeiro
        # sinal de regressão.
        assert r.status_code == 200, r.text
        assert _FakeRetriever.last_kwargs is not None
        assert _FakeRetriever.last_kwargs["query"] == "faturamento"
        assert _FakeRetriever.last_kwargs["top_n"] == 5
        assert _FakeRetriever.last_kwargs["allowed_source_ids"] == [self.KS]

    def test_returns_results_with_expected_shape(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "x"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["source_id"] == self.KS
        assert body["query"] == "x"
        assert body["count"] == 1
        assert body["results"][0]["evidence_id"] == "ev-1"
        assert body["results"][0]["relevance_score"] == 0.42
        assert body["results"][0]["source_name"] == "FAQ Claro"

    def test_returns_empty_list_when_retriever_finds_nothing(self, client):
        _FakeRetriever.next_results = []
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "termo_inexistente"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["results"] == []

    def test_404_when_ks_not_found(self, client):
        r = client.post(
            "/api/v1/knowledge-sources/ghost/query",
            json={"query": "x"},
        )
        assert r.status_code == 404
        # Não deve ter chegado ao retriever
        assert _FakeRetriever.last_kwargs is None

    def test_400_when_query_is_empty_string(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": ""},
        )
        assert r.status_code == 400

    def test_400_when_query_is_whitespace_only(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "   \t\n"},
        )
        assert r.status_code == 400

    def test_query_is_trimmed_before_passing_to_retriever(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "  faturamento  "},
        )
        assert r.status_code == 200
        assert _FakeRetriever.last_kwargs["query"] == "faturamento"

    def test_top_n_capped_at_20_max(self, client):
        """top_n > 20 deve ser clampado para 20 (proteção de carga)."""
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "x", "top_n": 9999},
        )
        assert r.status_code == 200
        assert _FakeRetriever.last_kwargs["top_n"] == 20

    def test_top_n_floored_at_1_min(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "x", "top_n": 0},
        )
        assert r.status_code == 200
        assert _FakeRetriever.last_kwargs["top_n"] == 1

    def test_top_n_defaults_to_5_when_absent(self, client):
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "x"},
        )
        assert r.status_code == 200
        assert _FakeRetriever.last_kwargs["top_n"] == 5

    def test_scope_restricted_to_this_ks_only(self, client):
        """allowed_source_ids deve ter exatamente [ks_id] — nunca a KB inteira
        ou outras KBs. Isolamento por inspeção é parte do contrato."""
        r = client.post(
            f"/api/v1/knowledge-sources/{self.KS}/query",
            json={"query": "x"},
        )
        assert r.status_code == 200
        assert _FakeRetriever.last_kwargs["allowed_source_ids"] == [self.KS]
