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
import pytest

from app.agents.engine import (
    CONDITIONAL_VARS_META,
    DeepAgentHarness,
    _build_conditional_context,
    _decision_vars_for_source,
    _eval_conditional,
    _expr_uses_output,
    _preserve_decision_line,
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
