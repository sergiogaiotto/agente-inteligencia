"""Contrato de Decisão ESTRUTURADO no envelope do invoke (36.1.0).

Backlog do arco condicional: a camada de apresentação remove a linha DECISAO
do texto — o consumidor MÁQUINA (X-API-Key, orquestradores externos) recebe a
decisão validada como `decision: {campo: valor}` no envelope, em todas as
verbosidades, sem parsear texto.
"""
import pytest

from app.agents.engine import extract_decision_for_agent
from app.agents.result_view import project_pipeline_result

SKILL_MD = """# Triagem
## Decisions
```json
{ "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
```
"""


class TestExtractDecisionForAgent:
    @pytest.mark.asyncio
    async def test_extrai_estruturado_quando_ha_contrato(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": "sk-1"}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        got = await extract_decision_for_agent(
            "Caso grave.\nDECISAO: escalar=SIM; severidade=Alta", "ag-1")
        assert got == {"escalar": "sim", "severidade": "alta"}  # canônico

    @pytest.mark.asyncio
    async def test_sem_contrato_ou_sem_linha_e_none(self, monkeypatch):
        import app.agents.engine as eng

        async def _agent(_id):
            return {"id": _id, "skill_id": ""}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        assert await extract_decision_for_agent("DECISAO: escalar=sim", "ag-x") is None
        assert await extract_decision_for_agent("resposta comum", "ag-x") is None
        assert await extract_decision_for_agent("", "ag-x") is None

    @pytest.mark.asyncio
    async def test_fail_safe(self, monkeypatch):
        import app.agents.engine as eng

        async def _boom(_id):
            raise RuntimeError("db off")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        assert await extract_decision_for_agent("DECISAO: escalar=sim", "ag-1") is None


class TestDecisionNasVerbosidades:
    RESULT = {
        "mode": "pipeline", "pipeline_id": "p1", "interaction_id": "i1",
        "status": "completed", "output": "Resposta limpa.",
        "decision": {"escalar": "sim"}, "final_state": "Recommend",
        "pipeline_steps": [], "total_agents": 1, "completed_agents": 1,
        "duration_ms": 10.0,
    }

    def test_summary_e_minimal_incluem_decision(self):
        # summary/minimal são os defaults de X-API-Key — o sinal de máquina
        # PRECISA estar neles (a linha não vem mais no texto).
        for v in ("summary", "minimal"):
            proj = project_pipeline_result(dict(self.RESULT), v)
            assert proj["decision"] == {"escalar": "sim"}, v

    def test_full_e_verbatim_com_decision(self):
        proj = project_pipeline_result(dict(self.RESULT), "full")
        assert proj["decision"] == {"escalar": "sim"}

    def test_sem_contrato_decision_none(self):
        r = dict(self.RESULT)
        r["decision"] = None
        for v in ("full", "summary", "minimal"):
            assert project_pipeline_result(dict(r), v)["decision"] is None, v


def test_agent_invoke_response_tem_campo_decision():
    from app.models.schemas import AgentInvokeResponse
    r = AgentInvokeResponse(agent_id="a", status="ok", decision={"escalar": "sim"})
    assert r.decision == {"escalar": "sim"}
    # aditivo: default None (consumidores antigos não quebram)
    assert AgentInvokeResponse(agent_id="a", status="ok").decision is None
