"""Roteamento rápido (PR-2 do tuning, 26.0.0).

Pula a chamada LLM do agente ENTRY (router) quando TODAS as arestas de saída
roteiam só por args selados + pergunta (nunca pelo output do router). A
elegibilidade estática é o coração da correção — um erro aqui rotearia errado.
"""
from __future__ import annotations

import json

import pytest

import app.agents.engine as engine


# ─── classificação de variáveis da expr ─────────────────────────────

class TestExprUsesOutput:
    @pytest.mark.parametrize("expr", [
        "inputs.tipo == 'limite'",
        "'pix' in input_lower",
        "inputs.cd_cliente > 0 and 'credito' in input_lower",
        "has_document",
        "",  # sem expr → não usa output
    ])
    def test_input_only_nao_usa_output(self, expr):
        assert engine._expr_uses_output(expr) is False

    @pytest.mark.parametrize("expr", [
        "'pix' in output_lower",
        "final_state == 'Recommend'",
        "is_escalate",
        "output_length > 100",
        "inputs.tipo == 'x' or 'y' in output_lower",  # mistura → depende de output
    ])
    def test_output_class_usa_output(self, expr):
        assert engine._expr_uses_output(expr) is True

    def test_expr_malformada_fail_safe_true(self):
        # não parseia → assume que depende (roda o LLM)
        assert engine._expr_uses_output("inputs.tipo ==") is True


# ─── elegibilidade do entry ─────────────────────────────────────────

def _patch_edges(monkeypatch, edges):
    async def fake_find_all(source_agent_id, limit):
        return edges
    import app.core.database as _db
    monkeypatch.setattr(_db.mesh_repo, "find_all", fake_find_all)
    # entry sem skill → guard de target estruturado inerte (isola a lógica de aresta)
    async def fake_topo_agent(aid):
        return {"id": aid, "skill_id": None}
    monkeypatch.setattr(engine, "_topo_agent", fake_topo_agent)
    engine._pipeline_topo.set(None)  # fora de cache → passa direto


def _cond(target, expr):
    return {"target_agent_id": target, "connection_type": "conditional",
            "config": json.dumps({"expr": expr})}


class TestEntryFastRoutable:
    @pytest.mark.asyncio
    async def test_todas_condicionais_input_only_eh_pulavel(self, monkeypatch):
        _patch_edges(monkeypatch, [
            _cond("a", "inputs.tipo == 'limite'"),
            _cond("b", "inputs.tipo == 'analise'"),
            {"target_agent_id": "c", "connection_type": "default", "config": "{}"},
        ])
        assert await engine._entry_fast_routable("entry") is True

    @pytest.mark.asyncio
    async def test_aresta_dependente_de_output_nao_pula(self, monkeypatch):
        _patch_edges(monkeypatch, [
            _cond("a", "inputs.tipo == 'limite'"),
            _cond("b", "'erro' in output_lower"),  # depende do output
        ])
        assert await engine._entry_fast_routable("entry") is False

    @pytest.mark.asyncio
    async def test_aresta_sequential_nao_pula(self, monkeypatch):
        _patch_edges(monkeypatch, [
            {"target_agent_id": "a", "connection_type": "sequential", "config": "{}"},
        ])
        assert await engine._entry_fast_routable("entry") is False

    @pytest.mark.asyncio
    async def test_conditional_sem_expr_nao_pula(self, monkeypatch):
        _patch_edges(monkeypatch, [
            {"target_agent_id": "a", "connection_type": "conditional", "config": "{}"},
        ])
        assert await engine._entry_fast_routable("entry") is False

    @pytest.mark.asyncio
    async def test_sem_arestas_nao_pula(self, monkeypatch):
        _patch_edges(monkeypatch, [])
        assert await engine._entry_fast_routable("entry") is False


# ─── guard do roteador de TARGET ESTRUTURADO (equivalência de rota) ──

class TestStructuredTargetGuard:
    """Achado adversarial ALTO: um router que emite {"target","inputs"} decide a
    rota pelo OUTPUT (override autoritativo sobre a expr). Pular o LLM apagaria
    esse sinal → rota poderia divergir mesmo com expr input-only. O guard recusa
    o fast-routing para esses routers (fail-safe: perde só o speedup)."""

    def _patch_skill(self, monkeypatch, output_contract, raw="x"):
        async def fake_find(sid):
            return {"raw_content": raw} if sid else None
        monkeypatch.setattr(engine.skills_repo, "find_by_id", fake_find)
        class _Parsed:
            pass
        p = _Parsed(); p.output_contract = output_contract
        monkeypatch.setattr(engine, "parse_skill_md", lambda _r: p)

    @pytest.mark.asyncio
    async def test_contrato_com_target_e_inputs_eh_estruturado(self, monkeypatch):
        self._patch_skill(monkeypatch, '```json\n{"target": "X", "inputs": {}}\n```')
        assert await engine._skill_emits_structured_target("s1") is True

    @pytest.mark.asyncio
    async def test_contrato_texto_puro_nao_eh_estruturado(self, monkeypatch):
        self._patch_skill(monkeypatch, "Responda em pt-BR, texto puro, sem JSON.")
        assert await engine._skill_emits_structured_target("s1") is False

    @pytest.mark.asyncio
    async def test_sem_skill_id_fail_safe_false(self):
        assert await engine._skill_emits_structured_target(None) is False

    @pytest.mark.asyncio
    async def test_entry_estruturado_nao_pula_mesmo_com_expr_input_only(self, monkeypatch):
        _patch_edges(monkeypatch, [
            _cond("a", "inputs.tipo == 'limite'"),
            _cond("b", "inputs.tipo == 'analise'"),
        ])
        async def is_struct(sid):
            return True   # router estruturado
        monkeypatch.setattr(engine, "_skill_emits_structured_target", is_struct)
        assert await engine._entry_fast_routable(
            "entry", entry_agent={"skill_id": "s1"}) is False

    @pytest.mark.asyncio
    async def test_entry_nao_estruturado_pula(self, monkeypatch):
        _patch_edges(monkeypatch, [_cond("a", "inputs.tipo == 'limite'")])
        async def is_struct(sid):
            return False
        monkeypatch.setattr(engine, "_skill_emits_structured_target", is_struct)
        assert await engine._entry_fast_routable(
            "entry", entry_agent={"skill_id": "s1"}) is True


# ─── contrato/UI ────────────────────────────────────────────────────

class TestFastRoutingWiring:
    def test_toggle_global_no_contrato(self):
        from app.core.config import (
            Settings, PARAMETER_UI_KEYS, _UI_TO_ENV_MAP, _NON_MODEL_UI_KEYS
        )
        assert Settings().fast_routing_enabled is False  # default OFF
        assert "fast_routing_enabled" in PARAMETER_UI_KEYS
        assert _UI_TO_ENV_MAP["fast_routing_enabled"] == "FAST_ROUTING_ENABLED"
        assert "fast_routing_enabled" in _NON_MODEL_UI_KEYS

    def test_pipeline_schema_e_serialize(self):
        from app.models.schemas import PipelineCreate, PipelineUpdate
        assert PipelineCreate(name="x").fast_routing is False
        assert PipelineUpdate(fast_routing=True).fast_routing is True
        from app.routes.pipelines import _serialize
        s = _serialize({"id": "p1", "name": "P", "fast_routing": 1}, [])
        assert s["fast_routing"] is True
        s2 = _serialize({"id": "p2", "name": "P", "fast_routing": 0}, [])
        assert s2["fast_routing"] is False

    def test_migracao_idempotente_da_coluna(self):
        from app.core.database import _IDEMPOTENT_MIGRATIONS, SCHEMA
        migs = "\n".join(_IDEMPOTENT_MIGRATIONS)
        assert "ALTER TABLE pipelines ADD COLUMN IF NOT EXISTS fast_routing" in migs
        assert "fast_routing INTEGER DEFAULT 0" in SCHEMA

    def test_gate_verifier_honra_sinal_explicito_de_step(self):
        # Fix 26.0.0: com fast-routing o downstream recebe pipeline_context=""
        # (router pulado). O gate combina `pipeline_step or bool(ctx)`, então:
        #  - step de pipeline c/ contexto VAZIO ⇒ auto-passa (não dispara juiz)
        assert engine._verify_autopass(True, False, "standard", True) is True
        #  - fora de pipeline, contexto vazio ⇒ NÃO auto-passa (roda verifier)
        assert engine._verify_autopass(False, False, "standard", True) is False
        #  - rigorous + v2 continua NÃO auto-passando mesmo sinalizado (audita)
        assert engine._verify_autopass(True, False, "rigorous", True) is False
        #  - skip_evidence sempre auto-passa
        assert engine._verify_autopass(False, True, "rigorous", True) is True

    def test_ui_toggle_no_estudio_e_nos_parametros(self):
        from pathlib import Path
        params = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
        assert "fast_routing_enabled" in params
        estudio = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
        assert "pipeline-fast-routing" in estudio        # toggle por pipeline
        assert "setFastRouting" in estudio                # salva via PUT
