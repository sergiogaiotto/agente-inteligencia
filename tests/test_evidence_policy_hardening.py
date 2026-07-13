"""Hardening do Evidence Policy/RAG — regressões do E2E "Pulsar Telecom"
(2026-07-13, revisão profunda pós-bateria #5).

Incidente que motivou o PR: um UUID digitado à mão no ``## Evidence Policy``
(transcrito errado de um screenshot) virou filtro SQL ``= ANY(...)`` que casa
0 linhas em AMBOS os braços da busca híbrida — sem log, sem warning, sem
validação em nenhuma camada. O agente só "recusava por falta de evidências"
e o trace ainda mostrava "evid 0.8" (hardcoded), mascarando o diagnóstico.

Cobertura:
1. POST /skills com source desconhecida no Evidence Policy → 201 com warning.
2. POST /skills com source conhecida → sem warning dessa classe.
3. PUT /skills/{id} com source desconhecida → warnings aditivos no retorno.
4. Retriever._diagnose_empty_filtered_result: loga retrieval.unknown_source /
   retrieval.unauthorized_source; best-effort (erro de banco não propaga).
5. pgvector search: ambos os branches exigem knowledge_sources.authorized=1
   (paridade com o BM25 — antes uma base desautorizada vazava pelo vetorial).
6. engine: o gate "zero evidências + profile não-rigorous" reporta
   confidence 0.0 (honesto) — nunca mais o 0.8 fantasma.

Mocks: repos e asyncpg pool via monkeypatch/AsyncMock — sem Postgres real
(mesmo padrão de test_skill_routes_db_errors.py e test_pgvector_store.py).
"""
from __future__ import annotations

import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import skills_repo
from app.evidence import pgvector_store
from app.evidence.runtime import Retriever
from app.routes import skills as skills_routes
from app.routes.skills import router as skills_router

GOOD_UUID = "fabeca4a-9d47-4637-88c6-200fb43c7b22"
BAD_UUID = "fabecd4a-9947-4637-88c6-288fb43c7b22"  # o UUID errado real do E2E


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(skills_router)
    return TestClient(app, raise_server_exceptions=False)


def _skill_md(source_id: str) -> str:
    return f"""---
id: urn:skill:telecom:subagent:pulsar-teste-hardening
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Pulsar Teste Hardening

## Purpose
Testa validação de sources.

## Activation Criteria
Sempre.

## Inputs
- mensagem

## Workflow
1. consulta a base

## Tool Bindings
Nenhuma.

## Output Contract
Texto.

## Failure Modes
- sem evidência: recusa

## Evidence Policy
```yaml
sources:
  - {source_id}
```
"""


def _mock_ks_query(monkeypatch, rows=None, error: Exception | None = None):
    """Mocka a query única `id = ANY($1)` do _warn_unknown_evidence_sources
    (Pacote A: o loop de find_by_id virou 1 round-trip)."""
    con = MagicMock()
    if error is not None:
        con.fetch = AsyncMock(side_effect=error)
    else:
        con.fetch = AsyncMock(return_value=rows or [])
    monkeypatch.setattr("app.routes.skills._get_pool", lambda: _pool_with_con(con))
    return con


class TestCreateSkillUnknownSourceWarning:
    def test_unknown_source_returns_201_with_warning(self, monkeypatch):
        """Source inexistente NÃO bloqueia o save (design non-blocking do
        editor), mas o 201 precisa avisar — antes era silêncio total."""
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        _mock_ks_query(monkeypatch, rows=[])

        r = _client().post("/api/v1/skills", json={"raw_content": _skill_md(BAD_UUID)})
        assert r.status_code == 201
        warnings = r.json()["warnings"]
        assert any(BAD_UUID in w for w in warnings), warnings
        assert "avisos" in r.json()["message"]

    def test_known_source_no_unknown_warning(self, monkeypatch):
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        _mock_ks_query(monkeypatch, rows=[{"id": GOOD_UUID, "name": "Pulsar", "authorized": 1}])

        r = _client().post("/api/v1/skills", json={"raw_content": _skill_md(GOOD_UUID)})
        assert r.status_code == 201
        assert not any("não existe em Bases" in w for w in r.json()["warnings"])

    def test_unauthorized_source_warns(self, monkeypatch):
        """Source existente porém authorized=0: BM25 e pgvector filtram
        authorized=1, então em runtime o efeito = UUID inexistente. O save
        precisa avisar (achado do review adversarial deste PR)."""
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        _mock_ks_query(monkeypatch, rows=[{"id": GOOD_UUID, "name": "Base Pulsar", "authorized": 0}])

        r = _client().post("/api/v1/skills", json={"raw_content": _skill_md(GOOD_UUID)})
        assert r.status_code == 201
        assert any("DESAUTORIZADA" in w for w in r.json()["warnings"])

    def test_duplicated_source_warns_once(self, monkeypatch):
        """sources: [X, X] com X inexistente → 1 warning, não 2 (dedup)."""
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        con = _mock_ks_query(monkeypatch, rows=[])
        md = _skill_md(BAD_UUID).replace(
            f"sources:\n  - {BAD_UUID}",
            f"sources:\n  - {BAD_UUID}\n  - {BAD_UUID}",
        )
        r = _client().post("/api/v1/skills", json={"raw_content": md})
        assert r.status_code == 201
        assert sum(1 for w in r.json()["warnings"] if BAD_UUID in w) == 1
        # e a validação foi UMA query (ANY), não N find_by_id
        assert con.fetch.await_count == 1

    def test_empty_sources_list_warns_blocking(self, monkeypatch):
        """Foot-gun `sources: []`: bloqueia TODO retrieval (≠ remover a seção,
        que libera todas as bases). O save agora avisa."""
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        _mock_ks_query(monkeypatch, rows=[])
        md = _skill_md(BAD_UUID).replace(f"sources:\n  - {BAD_UUID}", "sources: []")
        r = _client().post("/api/v1/skills", json={"raw_content": md})
        assert r.status_code == 201
        assert any("BLOQUEIA todo o retrieval" in w for w in r.json()["warnings"])

    def test_db_failure_in_validation_never_blocks_save(self, monkeypatch):
        """Best-effort: banco fora do ar na validação não pode derrubar o save."""
        monkeypatch.setattr(skills_repo, "create", AsyncMock(return_value=None))
        _mock_ks_query(monkeypatch, error=Exception("postgres offline"))

        r = _client().post("/api/v1/skills", json={"raw_content": _skill_md(BAD_UUID)})
        assert r.status_code == 201


class TestUpdateSkillUnknownSourceWarning:
    def test_put_returns_additive_warnings(self, monkeypatch):
        """Quem digita UUID à mão edita no PUT — o retorno agora carrega
        `warnings` (o PUT não devolvia aviso NENHUM antes deste PR)."""
        monkeypatch.setattr(
            skills_repo, "find_by_id",
            AsyncMock(return_value={"id": "s1", "version": "0.1.0", "tags": "[]"}),
        )
        monkeypatch.setattr(
            skills_repo, "update", AsyncMock(return_value={"id": "s1", "version": "0.1.1"}),
        )
        _mock_ks_query(monkeypatch, rows=[])

        r = _client().put("/api/v1/skills/s1", json={"raw_content": _skill_md(BAD_UUID)})
        assert r.status_code == 200
        body = r.json()
        assert any(BAD_UUID in w for w in body.get("warnings", [])), body

    def test_put_syncs_frontmatter_version_with_bump(self, monkeypatch):
        """Pacote A5: o bump da coluna (0.1.0→0.1.1) agora reescreve também o
        `version:` do frontmatter no raw_content persistido — antes divergiam
        para sempre (coluna v0.1.2, YAML 0.1.0)."""
        monkeypatch.setattr(
            skills_repo, "find_by_id",
            AsyncMock(return_value={"id": "s1", "version": "0.1.3", "tags": "[]"}),
        )
        captured = {}

        async def _capture_update(_id, data):
            captured.update(data)
            return {"id": _id, **data}

        monkeypatch.setattr(skills_repo, "update", _capture_update)
        _mock_ks_query(monkeypatch, rows=[{"id": BAD_UUID, "name": "X", "authorized": 1}])

        r = _client().put("/api/v1/skills/s1", json={"raw_content": _skill_md(BAD_UUID)})
        assert r.status_code == 200
        assert captured["version"] == "0.1.4"
        assert "version: 0.1.4" in captured["raw_content"]
        assert "version: 0.1.0" not in captured["raw_content"]


def _pool_with_con(con):
    pool = MagicMock()

    class _Ctx:
        async def __aenter__(self_):
            return con

        async def __aexit__(self_, *a):
            return False

    pool.acquire = MagicMock(return_value=_Ctx())
    return pool


class TestDiagnoseEmptyFilteredResult:
    @pytest.mark.asyncio
    async def test_unknown_source_logs_event(self, monkeypatch, caplog):
        con = MagicMock()
        con.fetch = AsyncMock(return_value=[])  # nenhum id encontrado
        monkeypatch.setattr(
            "app.evidence.runtime._get_pool", lambda: _pool_with_con(con)
        )
        span = MagicMock()
        with caplog.at_level(logging.WARNING, logger="app.evidence.runtime"):
            await Retriever()._diagnose_empty_filtered_result([BAD_UUID], span)
        events = [r.__dict__.get("event") for r in caplog.records]
        assert "retrieval.unknown_source" in events
        span.set_attribute.assert_any_call("retriever.unknown_sources", BAD_UUID)

    @pytest.mark.asyncio
    async def test_unauthorized_source_logs_event(self, monkeypatch, caplog):
        con = MagicMock()
        con.fetch = AsyncMock(return_value=[{"id": GOOD_UUID, "authorized": 0}])
        monkeypatch.setattr(
            "app.evidence.runtime._get_pool", lambda: _pool_with_con(con)
        )
        with caplog.at_level(logging.WARNING, logger="app.evidence.runtime"):
            await Retriever()._diagnose_empty_filtered_result([GOOD_UUID], MagicMock())
        events = [r.__dict__.get("event") for r in caplog.records]
        assert "retrieval.unauthorized_source" in events
        assert "retrieval.unknown_source" not in events

    @pytest.mark.asyncio
    async def test_db_error_never_propagates(self, monkeypatch):
        con = MagicMock()
        con.fetch = AsyncMock(side_effect=Exception("boom"))
        monkeypatch.setattr(
            "app.evidence.runtime._get_pool", lambda: _pool_with_con(con)
        )
        # não pode levantar — o diagnóstico é best-effort dentro do search()
        await Retriever()._diagnose_empty_filtered_result([BAD_UUID], MagicMock())


class TestPgvectorAuthorizedParity:
    """Antes: só o BM25 exigia ks.authorized=1 (JOIN). Uma base desautorizada
    continuava recuperável pelo braço vetorial — assimetria de autorização."""

    @pytest.mark.asyncio
    async def test_filtered_branch_requires_authorized(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = MagicMock()
        con.fetchrow = AsyncMock(return_value={"atttypmod": 1024})
        con.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: _pool_with_con(con))

        await pgvector_store.search([0.1] * 1024, top_n=5, source_ids=["s1"])
        sql = str(con.fetch.await_args.args[0]).lower()
        assert "authorized = 1" in sql
        assert "any(" in sql

    @pytest.mark.asyncio
    async def test_unfiltered_branch_requires_authorized(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        con = MagicMock()
        con.fetchrow = AsyncMock(return_value={"atttypmod": 1024})
        con.fetch = AsyncMock(return_value=[])
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: _pool_with_con(con))

        await pgvector_store.search([0.1] * 1024, top_n=5)
        sql = str(con.fetch.await_args.args[0]).lower()
        assert "authorized = 1" in sql


class TestHonestZeroEvidenceConfidence:
    def test_engine_no_longer_fakes_confidence_08(self):
        """Regressão textual: o gate "zero evidências + não-rigorous" reportava
        ``confidence: 0.8`` hardcoded INCONDICIONAL — o trace exibia "evid 0.8"
        com retrieval VAZIO e mascarou o diagnóstico do E2E Pulsar. Agora o
        score é condicionado a grounding não-RAG (anexo/tool → 0.8; nada →
        0.0), preservando a semântica do Cockpit ("evidence_score > 0: RAG,
        anexo ou tool"). O call-site vive inline numa função de milhares de
        linhas (execute_interaction), então a regressão mais barata e estável
        é textual no call-site exato."""
        src = pathlib.Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert 'run_verify_evidence({"ok": True, "confidence": 0.8})' not in src
        assert '0.8 if _nonrag_grounding else 0.0' in src
