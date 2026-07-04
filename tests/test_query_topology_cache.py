"""Cache de topologia/schema do invoke (PR-1 do tuning, 25.2.0).

- `_order_col` memoiza a coluna de ORDER BY por tabela (elimina a
  introspeção em information_schema por find_all);
- os helpers `_topo_mesh_out`/`_topo_agent` memoizam mesh/agents por
  requisição de pipeline (contextvar), e PASSAM DIRETO fora de pipeline
  (contextvar None) — zero mudança de comportamento;
- tudo gated pelo toggle `query_topology_cache_enabled` (default Sim).
"""
from __future__ import annotations

import pytest

import app.core.database as db
import app.agents.engine as engine


# ─── _order_col cacheado ────────────────────────────────────────────

class _FakeCon:
    def __init__(self):
        self.introspections = 0

    async def fetch(self, sql, *a):
        if "information_schema" in sql:
            self.introspections += 1
            return [{"column_name": "created_at"}, {"column_name": "id"}]
        return []


class TestOrderColCache:
    @pytest.mark.asyncio
    async def test_segunda_chamada_nao_introspeciona(self, monkeypatch):
        db._ORDER_COL_CACHE.clear()
        monkeypatch.setattr(db, "_topology_cache_on", lambda: True)
        repo = db.Repository("verifications")
        con = _FakeCon()
        c1 = await repo._order_col(con)
        c2 = await repo._order_col(con)
        assert c1 == c2 == "created_at"
        assert con.introspections == 1  # só a 1ª consulta o schema

    @pytest.mark.asyncio
    async def test_toggle_off_sempre_introspeciona(self, monkeypatch):
        db._ORDER_COL_CACHE.clear()
        monkeypatch.setattr(db, "_topology_cache_on", lambda: False)
        repo = db.Repository("verifications")
        con = _FakeCon()
        await repo._order_col(con)
        await repo._order_col(con)
        assert con.introspections == 2  # sem cache, consulta toda vez

    def test_toggle_default_sim(self):
        from app.core.config import Settings
        assert Settings().query_topology_cache_enabled is True

    def test_toggle_no_contrato_de_parametros(self):
        from app.core.config import (
            PARAMETER_UI_KEYS, _UI_TO_ENV_MAP, _NON_MODEL_UI_KEYS, _SEALED_ENV_VARS
        )
        k = "query_topology_cache_enabled"
        assert k in PARAMETER_UI_KEYS
        assert _UI_TO_ENV_MAP[k] == "QUERY_TOPOLOGY_CACHE_ENABLED"
        assert k in _NON_MODEL_UI_KEYS  # env continua fallback
        assert _UI_TO_ENV_MAP[k] not in _SEALED_ENV_VARS


# ─── helpers de topologia ───────────────────────────────────────────

class TestTopoHelpers:
    @pytest.mark.asyncio
    async def test_passa_direto_fora_de_pipeline(self, monkeypatch):
        # contextvar None (fora de pipeline) → chama o repo, sem memoizar
        calls = {"mesh": 0, "agent": 0}

        async def fake_mesh(source_agent_id, limit):
            calls["mesh"] += 1
            return [{"id": "e1"}]

        async def fake_agent(aid):
            calls["agent"] += 1
            return {"id": aid}

        import app.core.database as _db
        monkeypatch.setattr(_db.mesh_repo, "find_all", fake_mesh)
        monkeypatch.setattr(_db.agents_repo, "find_by_id", fake_agent)
        engine._pipeline_topo.set(None)

        await engine._topo_mesh_out("a1")
        await engine._topo_mesh_out("a1")
        await engine._topo_agent("x")
        await engine._topo_agent("x")
        assert calls == {"mesh": 2, "agent": 2}  # sem cache fora de pipeline

    @pytest.mark.asyncio
    async def test_memoiza_dentro_de_pipeline(self, monkeypatch):
        calls = {"mesh": 0, "agent": 0}

        async def fake_mesh(source_agent_id, limit):
            calls["mesh"] += 1
            return [{"id": "e1"}]

        async def fake_agent(aid):
            calls["agent"] += 1
            return {"id": aid}

        import app.core.database as _db
        monkeypatch.setattr(_db.mesh_repo, "find_all", fake_mesh)
        monkeypatch.setattr(_db.agents_repo, "find_by_id", fake_agent)
        token = engine._pipeline_topo.set({"mesh": {}, "agents": {}})
        try:
            await engine._topo_mesh_out("a1")
            await engine._topo_mesh_out("a1")  # cache hit
            await engine._topo_agent("x")
            await engine._topo_agent("x")      # cache hit
            assert calls == {"mesh": 1, "agent": 1}  # cada id só 1 vez
        finally:
            engine._pipeline_topo.reset(token)

    @pytest.mark.asyncio
    async def test_contextvar_reseta_em_raise_do_setup(self, monkeypatch):
        # BUG #1 da revisão: raise inicial (agente não encontrado) deve
        # resetar o contextvar (higiene), não vazar para o resto do contexto.
        import app.core.database as _db
        monkeypatch.setattr(_db, "_topology_cache_on", lambda: True)

        async def fake_agent(aid):
            return None  # força o raise "agente não encontrado"
        monkeypatch.setattr(_db.agents_repo, "find_by_id", fake_agent)

        engine._pipeline_topo.set(None)
        with pytest.raises(ValueError):
            await engine.execute_pipeline("inexistente", "oi")
        assert engine._pipeline_topo.get() is None, "contextvar vazou após o raise"

    @pytest.mark.asyncio
    async def test_agent_retorna_copia_mutacao_nao_corrompe_cache(self, monkeypatch):
        # _run_llm_chain muta agent['llm_provider']; a cópia protege o cache.
        async def fake_agent(aid):
            return {"id": aid, "llm_provider": "gpt-oss-120b"}
        import app.core.database as _db
        monkeypatch.setattr(_db.agents_repo, "find_by_id", fake_agent)
        token = engine._pipeline_topo.set({"mesh": {}, "agents": {}})
        try:
            a1 = await engine._topo_agent("x")
            a1["llm_provider"] = "azure"       # simula mutação de fallback
            a2 = await engine._topo_agent("x")  # cache hit
            assert a2["llm_provider"] == "gpt-oss-120b"  # não corrompeu
            assert a1 is not a2
        finally:
            engine._pipeline_topo.reset(token)
