"""Regressão de eventos estruturados via caplog (retroativo).

Convenção (docs/troubleshooting.md): cada `logger.warning(..., extra={"event": "..."})`
ou similar precisa de teste asserindo que o evento sai com o nome e extras
corretos. Pega:

- Typo no `event=` (ex: 'qdrant.upsert.failed' → 'qdrant.upsert.fail')
- Esquecimento de campo extra crítico (qdrant_url, error_type, source_ids, etc)
- Mudança acidental de nível (WARNING → INFO)

Este arquivo cobre os eventos JÁ ENTREGUES em PRs anteriores que não tiveram
teste caplog na época. Eventos novos a partir daqui devem nascer com teste no
arquivo de teste do próprio módulo (ver docs/troubleshooting.md).

Cobertura inicial:
- pgvector_store: dim_mismatch, upsert.failed, column.recreated
- wizard: llm.resolved (3 paths)
- verifier: contract.retry_initiated, retry_succeeded, retry_failed_final
- ingest: evidence.ingest.partial, evidence.ingest.completed

Onda Q (2026-05-30): removida classe TestQdrantStoreEvents (qdrant_store
deletado). Eventos qdrant.* não existem mais. pgvector continua coberto.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import config as _config


@pytest.fixture
def fresh_settings(monkeypatch):
    _config.get_settings.cache_clear()
    yield
    _config.get_settings.cache_clear()


def _find_event(caplog, event_name: str):
    """Helper: extrai LogRecord cujo extra event == event_name. None se ausente."""
    return next(
        (r for r in caplog.records if getattr(r, "event", "") == event_name),
        None,
    )


# ═════════════════════════════════════════════════════════════════
# qdrant_store — REMOVIDO em Onda Q (2026-05-30)
# Classe TestQdrantStoreEvents removida junto com o módulo.
# Eventos qdrant.* não existem mais.
# ═════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════
# pgvector_store — PR D (#141)
# ═════════════════════════════════════════════════════════════════


def _make_pool_returning_dim(current_dim: int | None, points_count: int = 0):
    """Mock pool/conn pra pgvector_store — devolve current_dim em _column_dim."""
    con = MagicMock()
    if current_dim is None:
        con.fetchrow = AsyncMock(return_value=None)
    else:
        con.fetchrow = AsyncMock(return_value={"atttypmod": current_dim})
    con.fetchval = AsyncMock(return_value=points_count)
    con.execute = AsyncMock(return_value="UPDATE 1")
    con.fetch = AsyncMock(return_value=[])

    class _Ctx:
        async def __aenter__(self_): return con
        async def __aexit__(self_, *a): return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx())
    return pool, con


class TestPgvectorStoreEvents:
    @pytest.mark.asyncio
    async def test_dim_mismatch_emits_event_with_provider_and_hint(
        self, monkeypatch, fresh_settings, caplog
    ):
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import pgvector_store

        pool, _ = _make_pool_returning_dim(current_dim=1536)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        with caplog.at_level(logging.ERROR, logger="app.evidence.pgvector_store"):
            ok = await pgvector_store.ensure_embedding_column()

        assert ok is False
        rec = _find_event(caplog, "pgvector.column.dim_mismatch")
        assert rec is not None
        assert rec.dim_actual == 1536
        assert rec.dim_expected == 1024
        assert rec.embedding_provider == "qwen3"
        assert "reindex" in rec.hint.lower()

    @pytest.mark.asyncio
    async def test_column_recreated_emits_event_with_before_after(
        self, monkeypatch, fresh_settings, caplog
    ):
        """recreate_embedding_column de sucesso loga
        event=pgvector.column.recreated com dim_before, dim_after, points_deleted."""
        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")

        from app.evidence import pgvector_store

        pool, _ = _make_pool_returning_dim(current_dim=1536, points_count=42)
        monkeypatch.setattr(pgvector_store, "_get_pool", lambda: pool)

        with caplog.at_level(logging.INFO, logger="app.evidence.pgvector_store"):
            res = await pgvector_store.recreate_embedding_column()

        assert res["ok"] is True
        rec = _find_event(caplog, "pgvector.column.recreated")
        assert rec is not None
        assert rec.dim_before == 1536
        assert rec.dim_after == 1024
        assert rec.points_deleted == 42


# ═════════════════════════════════════════════════════════════════
# wizard — PR #146
# ═════════════════════════════════════════════════════════════════


class TestWizardLLMResolvedEvents:
    """wizard.llm.resolved tem 3 sources possíveis: task_type, legacy_explicit, route_default.
    Cada um deve emitir o evento com `source=` correto e provider+model + wizard_route."""

    @pytest.mark.asyncio
    async def test_explicit_task_type_logs_source_task_type(self, monkeypatch, fresh_settings, caplog):
        from app.routes.wizard import WizardSkillRequest, _resolve_wizard_llm

        async def _fake_resolve(task_type, has_image=False):
            return ("openai", "gpt-oss-120b")

        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        with caplog.at_level(logging.INFO, logger="app.routes.wizard"):
            await _resolve_wizard_llm(
                WizardSkillRequest(description="x", task_type="reasoning"),
                "skill",
            )

        rec = _find_event(caplog, "wizard.llm.resolved")
        assert rec is not None
        assert rec.source == "task_type"
        assert rec.task_type == "reasoning"
        assert rec.provider == "openai"
        assert rec.wizard_route == "skill"

    @pytest.mark.asyncio
    async def test_legacy_provider_logs_source_legacy_explicit(self, monkeypatch, fresh_settings, caplog):
        from app.routes.wizard import WizardAgentRequest, _resolve_wizard_llm

        async def _should_not_call(task_type, has_image=False):
            raise AssertionError("legacy path não deveria chamar resolver")

        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _should_not_call)

        with caplog.at_level(logging.INFO, logger="app.routes.wizard"):
            await _resolve_wizard_llm(
                WizardAgentRequest(description="x", provider="maritaca", model="sabia-3"),
                "agent",
            )

        rec = _find_event(caplog, "wizard.llm.resolved")
        assert rec is not None
        assert rec.source == "legacy_explicit"
        assert rec.provider == "maritaca"

    @pytest.mark.asyncio
    async def test_route_default_logs_source_route_default(self, monkeypatch, fresh_settings, caplog):
        from app.routes.wizard import WizardSkillRequest, _resolve_wizard_llm

        async def _fake_resolve(task_type, has_image=False):
            return ("any", "model")

        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        with caplog.at_level(logging.INFO, logger="app.routes.wizard"):
            await _resolve_wizard_llm(WizardSkillRequest(description="x"), "skill")

        rec = _find_event(caplog, "wizard.llm.resolved")
        assert rec is not None
        assert rec.source == "route_default"
        # Default da rota /skill mudou de "reasoning" pra "skill_generation"
        # em 2026-05-29 — separado após bugs Context7 #1-#4 (gpt-oss-120b
        # errando consistentemente as regras estruturais).
        assert rec.task_type == "skill_generation"


# ═════════════════════════════════════════════════════════════════
# verifier — PR #149
# ═════════════════════════════════════════════════════════════════


_SIMPLE_CONTRACT = """```json
{
  "type": "object",
  "title": "TestOutput",
  "required": ["answer"],
  "properties": {"answer": {"type": "string"}}
}
```"""


def _mock_provider_returning(content: str):
    p = MagicMock()
    p.supports_structured_output = False
    p.generate = AsyncMock(return_value={"content": content, "model": "fake", "usage": {}})
    return p


class TestVerifierContractRetryEvents:
    @pytest.fixture(autouse=True)
    def _v2_on(self, monkeypatch, fresh_settings):
        monkeypatch.setenv("VERIFIER_V2_ENABLED", "true")
        yield

    @pytest.mark.asyncio
    async def test_retry_succeeded_emits_event(self, monkeypatch, caplog):
        from app.verifier.runtime import Verifier

        fake = _mock_provider_returning('{"answer": "corrigido"}')
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        with caplog.at_level(logging.INFO, logger="app.verifier.runtime"):
            await v.verify(
                draft='{"foo": "bar"}',  # viola required: answer
                output_contract=_SIMPLE_CONTRACT,
                user_question="x",
                profile="fast",
                persist=False,
                llm_provider_name="azure",
                llm_model="gpt-4o",
            )

        # Sequência esperada: retry_initiated → retry_succeeded
        rec_init = _find_event(caplog, "verifier.contract.retry_initiated")
        rec_ok = _find_event(caplog, "verifier.contract.retry_succeeded")
        assert rec_init is not None, "retry_initiated não emitido"
        assert rec_ok is not None, "retry_succeeded não emitido"
        assert rec_init.llm_provider == "azure"
        assert rec_init.llm_model == "gpt-4o"
        assert len(rec_init.first_attempt_errors) > 0

    @pytest.mark.asyncio
    async def test_retry_failed_final_emits_event_with_both_error_lists(
        self, monkeypatch, caplog
    ):
        from app.verifier.runtime import Verifier

        fake = _mock_provider_returning('{"still": "broken"}')  # ainda inválido
        monkeypatch.setattr("app.core.llm_providers.get_provider", lambda *a, **kw: fake)
        monkeypatch.setattr("app.verifier.runtime.Verifier._extract_scores",
                            staticmethod(lambda d: {}))

        v = Verifier()
        with caplog.at_level(logging.WARNING, logger="app.verifier.runtime"):
            await v.verify(
                draft='{"foo": "bar"}',
                output_contract=_SIMPLE_CONTRACT,
                user_question="x",
                profile="fast",
                persist=False,
                llm_provider_name="azure",
            )

        rec = _find_event(caplog, "verifier.contract.retry_failed_final")
        assert rec is not None
        assert len(rec.original_errors) > 0
        assert len(rec.retry_errors) > 0


# ═════════════════════════════════════════════════════════════════
# ingest — PR #140
# ═════════════════════════════════════════════════════════════════


class TestEvidenceIngestEvents:
    """Os eventos evidence.ingest.* são emitidos no fim de ingest_text.
    Aqui validamos só o evento partial — o completed precisa de Postgres real."""

    @pytest.mark.asyncio
    async def test_partial_event_emits_when_vector_upsert_diverges(
        self, monkeypatch, fresh_settings, caplog
    ):
        """Simula o cenário do screenshot reportado: chunks no Postgres mas
        vetores divergentes. Evento partial sai com backend, source_id,
        contagens e hint."""
        # Mock todo o pipeline pra evitar Postgres real
        from app.evidence import ingest as ingest_mod

        monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen3")
        monkeypatch.setenv("QWEN3_DIMENSIONS", "0")
        monkeypatch.setenv("RAG_VECTOR_BACKEND", "qdrant")

        # knowledge_repo.find_by_id retorna source válida
        from app.core.database import knowledge_repo
        monkeypatch.setattr(
            knowledge_repo, "find_by_id",
            AsyncMock(return_value={"id": "ks-1", "name": "Test KS"})
        )
        monkeypatch.setattr(
            knowledge_repo, "update",
            AsyncMock(return_value=None)
        )

        # chunker
        fake_chunk = SimpleNamespace(text="a", ordinal=0, token_count=1, char_count=1)
        monkeypatch.setattr(ingest_mod, "chunk_text", lambda t, **kw: [fake_chunk])

        # embed_texts retorna 1 vetor
        monkeypatch.setattr(
            ingest_mod, "embed_texts",
            AsyncMock(return_value=[[0.1] * 1024])
        )

        # Pool fake (DELETE + INSERT no-op)
        fake_con = MagicMock()
        fake_con.execute = AsyncMock(return_value="INSERT 0 1")

        class _PoolCtx:
            async def __aenter__(self_): return fake_con
            async def __aexit__(self_, *a): return False

        class _Tx:
            async def __aenter__(self_): return None
            async def __aexit__(self_, *a): return False

        fake_con.transaction = MagicMock(return_value=_Tx())
        fake_pool = MagicMock()
        fake_pool.acquire = MagicMock(return_value=_PoolCtx())
        monkeypatch.setattr(ingest_mod, "_get_pool", lambda: fake_pool)

        # Vector store mockado: upsert retorna 0 (divergência) e delete OK
        fake_store = MagicMock()
        fake_store.upsert_chunks = AsyncMock(return_value=0)  # ← divergente
        fake_store.delete_by_source = AsyncMock(return_value=True)
        monkeypatch.setattr(ingest_mod, "_get_vector_store", lambda: fake_store)

        with caplog.at_level(logging.WARNING, logger="app.evidence.ingest"):
            result = await ingest_mod.ingest_text(
                source_id="ks-1",
                text="texto qualquer",
            )

        assert result["partial"] is True
        rec = _find_event(caplog, "evidence.ingest.partial")
        assert rec is not None, "evento evidence.ingest.partial não emitido"
        # Onda Q (2026-05-30): backend único pgvector (era 'qdrant').
        assert rec.rag_vector_backend == "pgvector"
        assert rec.source_id == "ks-1"
        assert rec.chunks_expected == 1
        assert rec.vector_upserted == 0
        # hint orienta o operador (mensagem pode citar pgvector ou só "vetores")
        assert "pgvector" in rec.hint.lower() or "vetor" in rec.hint.lower()
