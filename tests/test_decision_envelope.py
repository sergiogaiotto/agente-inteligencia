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


# ─── as ROTAS repassam o sinal (major do review: whitelists sem 'decision') ───

class TestDecisionAtravessaAsRotas:
    """O review pré-push pegou: os testes anteriores alimentavam o PROJETOR
    direto — as whitelists das rotas (sync, SSE, worker 202) descartavam a
    chave e o endpoint canônico de máquina devolvia decision:null. Aqui o
    payload passa pelas montagens REAIS."""

    def test_rota_sync_do_invoke_repassa_decision(self, monkeypatch):
        # TestClient na rota REAL (execute_pipeline mockado) → o payload passa
        # pela whitelist de verdade. Auth via dependency override (padrão do
        # test_invoke_verbosity).
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app.core.database as db
        import app.agents.engine as engine
        import app.catalog.pipeline_defs as pdefs
        import app.routes.pipelines as pl_routes

        def _async(v):
            async def _fn(*a, **k):
                return v
            return _fn

        result = {
            "pipeline_id": "p1", "status": "completed", "output": "texto limpo",
            "final_state": "Recommend", "interaction_id": "i1",
            "total_agents": 1, "completed_agents": 1, "pipeline_steps": [],
            "duration_ms": 5, "decision": {"escalar": "sim", "severidade": "alta"},
        }
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            _async({"id": "p1", "name": "F", "status": "publicado"}))
        monkeypatch.setattr(pdefs, "_build_subgraph",
                            _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
        monkeypatch.setattr(engine, "execute_pipeline", _async(result))
        monkeypatch.setattr(db.audit_repo, "create", _async({}))

        app = FastAPI()
        app.include_router(pl_routes.router)
        app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test", "role": "admin"}
        c = TestClient(app, raise_server_exceptions=False)
        for v in ("summary", "minimal", "full"):
            r = c.post("/api/v1/pipelines/p1/invoke", json={"message": "oi", "verbosity": v})
            assert r.status_code == 200, r.text
            assert r.json()["decision"] == {"escalar": "sim", "severidade": "alta"}, v

    def test_payload_do_job_202_persiste_decision(self):
        # a whitelist do worker é a ÚNICA via do consumidor async (polling);
        # sem a chave o dado se perdia PERMANENTEMENTE (texto já strippado).
        import inspect
        from app.core import invoke_jobs
        src = inspect.getsource(invoke_jobs)
        i_payload = src.find("payload_full = {")
        assert i_payload != -1
        assert '"decision": r.get("decision")' in src[i_payload:i_payload + 900]

    def test_evento_sse_pipeline_done_repassa_decision(self):
        import inspect
        from app.routes import pipelines as pr
        src = inspect.getsource(pr)
        i_cb = src.find("async def _cb(event: dict)")
        assert i_cb != -1
        assert '"decision": res.get("decision")' in src[i_cb:i_cb + 1200]


class TestDecisionFallbackDaCadeia:
    @pytest.mark.asyncio
    async def test_pega_a_decisao_mais_recente_da_cadeia(self, monkeypatch):
        # topologia comum: triagem (contrato) decide → especialista (sem
        # contrato) responde. O envelope carrega a decisão que ROTEOU.
        import app.agents.engine as eng

        async def _agent(aid):
            return {"id": aid, "skill_id": "sk-1" if aid == "ag-triagem" else ""}

        async def _skill(_id):
            return {"id": _id, "raw_content": SKILL_MD}

        monkeypatch.setattr(eng, "_topo_agent", _agent)
        monkeypatch.setattr(eng.skills_repo, "find_by_id", _skill)
        steps = [
            {"agent_id": "ag-triagem", "status": "completed",
             "output": "Caso grave.\nDECISAO: escalar=sim; severidade=alta"},
            {"agent_id": "ag-esp", "status": "completed",
             "output": "Vou priorizar seu caso imediatamente."},
        ]
        got = await eng._decision_from_steps(steps)
        assert got == {"escalar": "sim", "severidade": "alta"}

    @pytest.mark.asyncio
    async def test_step_pulado_e_cadeia_sem_linha_dao_none(self, monkeypatch):
        import app.agents.engine as eng

        async def _boom(_id):
            raise AssertionError("sem linha não deveria tocar o banco")

        monkeypatch.setattr(eng, "_topo_agent", _boom)
        steps = [
            {"agent_id": "a", "status": "skipped", "output": "DECISAO: escalar=sim"},
            {"agent_id": "b", "status": "completed", "output": "resposta comum"},
        ]
        assert await eng._decision_from_steps(steps) is None
