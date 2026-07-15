"""Contrato de Decisão — wiring no engine (Cond-C, 35.19.0).

O parser vive em app/skill_parser/decisions_schema.py (test_decisions_schema).
Aqui testamos os 4 pontos de acoplamento no engine:
  1. `decision.<campo>` disponível no contexto condicional (gate) — presente,
     ausente-seguro e canônico;
  2. `decision` é classe-OUTPUT (fast-routing NÃO pode pular o router quando
     uma aresta usa decision.* — a linha DECISAO só existe se o LLM rodar);
  3. `_decision_vars_for_source` — resolve schema do source e valida a linha,
     SEM tocar o banco quando o output não tem a linha (caso comum);
  4. `_build_system_prompt` injeta a diretiva selada quando a skill declara
     ## Decisions; `_preserve_decision_line` salva a linha do hard-truncate.
"""
import json
import logging

import pytest

from app.agents.engine import (
    CONDITIONAL_VARS_META,
    DeepAgentHarness,
    _build_conditional_context,
    _build_response_language_closing,
    _build_response_language_directive,
    _decision_vars_for_source,
    _decisions_schema_for_agent,
    _eval_conditional,
    _expr_uses_output,
    _preserve_decision_line,
    _should_skip_conditional,
    strip_decision_line_for_display,
)

SCHEMA = {"escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"]}


# ─── 1. decision no contexto do gate ─────────────────────────────────────────

class TestDecisionInConditionalContext:
    def test_regra_casa_valor_anunciado(self):
        ctx = _build_conditional_context(decision={"escalar": "sim"})
        assert _eval_conditional("decision.escalar == 'sim'", ctx) is True
        assert _eval_conditional("decision.escalar == 'não'", ctx) is False

    def test_campo_ausente_e_comparacao_segura(self):
        # campo não anunciado: não casa em NENHUM operador (sentinel), não estoura
        ctx = _build_conditional_context(decision={"escalar": "sim"})
        assert _eval_conditional("decision.severidade == 'alta'", ctx) is False
        assert _eval_conditional("decision.severidade > 1", ctx) is False
        assert _eval_conditional("'x' in decision.severidade", ctx) is False

    def test_sem_decision_default_vazio(self):
        ctx = _build_conditional_context()
        assert ctx["decision"] == {}
        assert _eval_conditional("decision.escalar == 'sim'", ctx) is False

    def test_meta_tem_decision(self):
        names = {v["name"] for v in CONDITIONAL_VARS_META}
        assert "decision" in names


# ─── 2. fast-routing: decision é classe-OUTPUT ───────────────────────────────

class TestDecisionIsOutputClass:
    def test_expr_com_decision_depende_do_output(self):
        # pular o LLM do router mataria a linha DECISAO → não pode fast-route
        assert _expr_uses_output("decision.escalar == 'sim'") is True

    def test_expr_input_only_segue_fast_routable(self):
        assert _expr_uses_output("inputs.tier == 'gold'") is False


# ─── 3. _decision_vars_for_source ────────────────────────────────────────────

SKILL_MD = """# Triagem
## Purpose
Triagem.
## Decisions
```json
{ "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
```
"""


class TestDecisionVarsForSource:
    @pytest.mark.asyncio
    async def test_sem_linha_nao_toca_o_banco(self, monkeypatch):
        import app.agents.engine as eng

        async def _boom(_id):  # o marker-check barato deve curto-circuitar
            raise AssertionError("não deveria buscar agente sem linha DECISAO")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        assert await _decision_vars_for_source("src-1", "resposta comum") == {}

    @pytest.mark.asyncio
    async def test_extrai_e_valida_contra_a_skill_do_source(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        got = await _decision_vars_for_source(
            "src-1", "Análise feita.\nDECISAO: escalar=SIM; severidade=Alta; fake=x"
        )
        # canônico + selado (campo fora do contrato descartado)
        assert got == {"escalar": "sim", "severidade": "alta"}

    @pytest.mark.asyncio
    async def test_source_sem_skill_ou_sem_contrato(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": ""}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        assert await _decision_vars_for_source("s", "DECISAO: escalar=sim") == {}

    @pytest.mark.asyncio
    async def test_erro_no_lookup_fail_safe(self, monkeypatch):
        import app.agents.engine as eng

        async def _boom(_id):
            raise RuntimeError("db off")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        assert await _decision_vars_for_source("s", "DECISAO: escalar=sim") == {}


# ─── 4. prompt selado + preservação no truncate ──────────────────────────────

class TestPromptInjectionAndTruncate:
    def _harness(self, skill_data: dict) -> DeepAgentHarness:
        # __new__ evita o provider real; _build_system_prompt só usa
        # config/mcp_tools.
        h = DeepAgentHarness.__new__(DeepAgentHarness)
        h.config = {"system_prompt": "Você é a triagem.", "_parsed_skill": skill_data}
        h.mcp_tools = []
        return h

    def test_injeta_diretiva_quando_skill_declara(self):
        h = self._harness({"purpose": "Triagem.", "_decisions_schema": SCHEMA})
        sp = h._build_system_prompt()
        assert "## Contrato de Decisão" in sp
        assert "DECISAO: escalar=<sim|não>; severidade=<baixa|média|alta>" in sp

    def test_nao_injeta_sem_contrato(self):
        h = self._harness({"purpose": "Triagem."})
        assert "Contrato de Decisão" not in h._build_system_prompt()

    def test_truncate_preserva_linha_decisao(self):
        original = "análise longa...\nDECISAO: escalar=sim; severidade=alta"
        truncated = "análise longa…"  # o hard-cut comeu a linha
        got = _preserve_decision_line(original=original, truncated=truncated, schema=SCHEMA)
        assert got.endswith("\nDECISAO: escalar=sim; severidade=alta")

    def test_truncate_sem_schema_ou_linha_intacta(self):
        assert _preserve_decision_line(original="x", truncated="y", schema=None) == "y"
        ja_tem = "resumo…\nDECISAO: escalar=sim"
        assert _preserve_decision_line(
            original="qualquer", truncated=ja_tem, schema=SCHEMA
        ) == ja_tem


# ─── 4b. colisão idioma × contrato (review 2026-07-15) ───────────────────────

class TestLanguageDirectiveDecisionException:
    def test_sem_contrato_diretivas_byte_identicas(self):
        # reprodutibilidade: sem ## Decisions o prompt não muda um byte — a
        # exceção é APPEND-ONLY sobre o texto de sempre.
        for lang in ("pt-BR", "en-US"):
            base_d = _build_response_language_directive(lang)
            base_c = _build_response_language_closing(lang)
            assert "DECISAO" not in base_d and "DECISAO" not in base_c
            assert _build_response_language_directive(
                lang, preserve_decision_line=True).startswith(base_d)
            assert _build_response_language_closing(
                lang, preserve_decision_line=True).startswith(base_c)

    def test_com_contrato_ambas_excepcionam_a_linha(self):
        d = _build_response_language_directive("en-US", preserve_decision_line=True)
        c = _build_response_language_closing("en-US", preserve_decision_line=True)
        assert "DECISAO" in d and "SEM traduzi-los" in d
        assert "DECISAO" in c and "NÃO" in c and "VERBATIM" in c

    def _harness(self, skill_data: dict) -> DeepAgentHarness:
        h = DeepAgentHarness.__new__(DeepAgentHarness)
        h.config = {"system_prompt": "Você é a triagem.", "_parsed_skill": skill_data}
        h.mcp_tools = []
        return h

    def test_system_prompt_com_contrato_tem_excecao_no_sanduiche(self):
        # o LEMBRETE FINAL é a última instrução antes da geração — é ele que
        # vence a atenção do modelo; a exceção precisa estar lá.
        sp = self._harness({"_decisions_schema": SCHEMA})._build_system_prompt()
        closing = sp.split("[LEMBRETE FINAL — IDIOMA]")[-1]
        assert "DECISAO" in closing

    def test_system_prompt_sem_contrato_sem_excecao(self):
        sp = self._harness({"purpose": "Triagem."})._build_system_prompt()
        assert "DECISAO" not in sp


# ─── 4c. telemetria: linha presente mas nada validou (review 2026-07-15) ─────

class TestDecisionLineInvalidTelemetry:
    @pytest.mark.asyncio
    async def test_valores_traduzidos_geram_warning(self, monkeypatch, caplog):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        with caplog.at_level(logging.WARNING):
            # agente en-US traduziu os valores: linha presente, enum rejeita tudo
            got = await _decision_vars_for_source("src-1", "Done.\nDECISAO: escalar=yes")
        assert got == {}
        assert any("decision_line_invalid" in r.message for r in caplog.records)


# ─── 4d. gate lexical + memoização do schema (review 2026-07-15) ─────────────

class TestGateLexicalAndMemo:
    @pytest.mark.asyncio
    async def test_expr_sem_decision_nao_paga_extracao(self, monkeypatch):
        import app.agents.engine as eng

        async def _conns(_id, limit=20):
            return [{
                "target_agent_id": "tgt-1", "connection_type": "conditional",
                "config": json.dumps({"expr": "'pix' in output_lower"}),
            }]

        async def _boom(*_a, **_k):
            raise AssertionError("expr sem decision.* não deveria extrair a linha")

        monkeypatch.setattr(eng, "_topo_mesh_out", _conns)
        monkeypatch.setattr(eng, "_decision_vars_for_source", _boom)
        # 'pix' não está no output → skip=True; e o boom prova que a extração
        # (lookup de skill + parse) não foi paga para uma regra que não usa decision.
        assert await _should_skip_conditional(
            source_id="s", target_id="tgt-1",
            last_output="Resposta.\nDECISAO: escalar=sim",  # linha presente, expr não usa
            last_final_state="Recommend",
        ) is True

    @pytest.mark.asyncio
    async def test_schema_memoizado_por_pipeline(self, monkeypatch):
        import app.agents.engine as eng

        calls = {"n": 0}

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            calls["n"] += 1
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        token = eng._pipeline_topo.set({"mesh": {}, "agents": {}})
        try:
            # fan-out de N arestas do mesmo source: find_by_id + parse UMA vez
            assert await _decisions_schema_for_agent("src-1") == SCHEMA
            assert await _decisions_schema_for_agent("src-1") == SCHEMA
        finally:
            eng._pipeline_topo.reset(token)
        assert calls["n"] == 1


# ─── 4e. strip da linha nas superfícies de apresentação ──────────────────────

class TestStripForDisplay:
    @pytest.mark.asyncio
    async def test_remove_linha_quando_agente_tem_contrato(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        got = await strip_decision_line_for_display(
            "Resposta ao cliente.\nDECISAO: escalar=sim; severidade=alta", "ag-1")
        assert got == "Resposta ao cliente."

    @pytest.mark.asyncio
    async def test_sem_contrato_prosa_decisao_fica_intacta(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": ""}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        txt = "Análise.\nDecisão: aprovado o crédito"
        assert await strip_decision_line_for_display(txt, "ag-legado") == txt

    @pytest.mark.asyncio
    async def test_fail_safe_erro_devolve_intacto(self, monkeypatch):
        import app.agents.engine as eng

        async def _boom(_id):
            raise RuntimeError("db off")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        txt = "Resposta.\nDECISAO: escalar=sim"
        assert await strip_decision_line_for_display(txt, "ag-1") == txt


# ─── 5. API: /mesh/agents/{id}/decisions + decision no simulador ─────────────

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def mesh_client():
    from app.routes.mesh import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestAgentDecisionsEndpoint:
    def test_devolve_contrato_declarado(self, mesh_client, monkeypatch):
        import app.core.database as db
        import app.routes.mesh as mesh_mod

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(mesh_mod.agents_repo, "find_by_id", _agent)
        monkeypatch.setattr(db.skills_repo, "find_by_id", _skill)
        r = mesh_client.get("/api/v1/mesh/agents/ag-1/decisions")
        assert r.status_code == 200
        assert r.json()["decisions"] == SCHEMA

    def test_sem_contrato_devolve_vazio(self, mesh_client, monkeypatch):
        import app.routes.mesh as mesh_mod

        async def _agent(_id):
            return {"id": _id, "skill_id": ""}  # agente sem skill

        monkeypatch.setattr(mesh_mod.agents_repo, "find_by_id", _agent)
        r = mesh_client.get("/api/v1/mesh/agents/ag-1/decisions")
        assert r.status_code == 200
        assert r.json()["decisions"] == {}

    def test_agente_inexistente_404(self, mesh_client, monkeypatch):
        import app.routes.mesh as mesh_mod

        async def _none(_id):
            return None

        monkeypatch.setattr(mesh_mod.agents_repo, "find_by_id", _none)
        assert mesh_client.get("/api/v1/mesh/agents/nope/decisions").status_code == 404


class TestTestConditionalDecision:
    def test_decision_explicito_simula_direto(self, mesh_client):
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "decision.escalar == 'sim'", "decision": {"escalar": "sim"}},
        )
        assert r.json()["result"] is True

    def test_sem_decision_regra_nao_casa_sem_estourar(self, mesh_client):
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={"expr": "decision.escalar == 'sim'", "output": "sem linha"},
        )
        body = r.json()
        assert body["result"] is False
        assert "error" not in body

    def test_source_agent_id_extrai_do_output_como_no_runtime(self, mesh_client, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        r = mesh_client.post(
            "/api/v1/mesh/connections/test-conditional",
            json={
                "expr": "decision.severidade == 'alta'",
                "output": "Análise.\nDECISAO: escalar=sim; severidade=ALTA",
                "source_agent_id": "ag-1",
            },
        )
        body = r.json()
        assert body["result"] is True
        # o contexto devolvido mostra a decisão CANÔNICA extraída (debug do operador)
        assert body["context"]["decision"] == {"escalar": "sim", "severidade": "alta"}
