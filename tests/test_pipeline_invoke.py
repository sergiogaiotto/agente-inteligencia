"""PR-A2 (Trilha A) — invoke por pipeline-entidade (contrato API-first selado).

POST /api/v1/pipelines/{id}/invoke resolve raiz+membros e executa via
execute_pipeline DELIMITADO ao subgrafo. aposentado→409; sem message→400;
sem raiz→422; happy→executa selado (allowed_agent_ids=membros).
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client(authed=True):
    app = FastAPI()
    app.include_router(pl_routes.router)
    if authed:
        # invoke agora EXIGE auth (cookie OU X-API-Key). Nos testes de COMPORTAMENTO
        # bypassamos a dependência; a auth em si é coberta por test_401_without_auth.
        app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test", "role": "admin"}
    return TestClient(app, raise_server_exceptions=False)


def _pipe(status="publicado"):
    return {"id": "p1", "name": "Folha", "status": status}


class TestInvokePipeline:
    def test_401_without_auth(self):
        # Contrato externo: sem cookie nem X-API-Key → 401 ANTES de qualquer
        # execução (require_user curto-circuita sem tocar no banco quando não há
        # credencial). Impede disparo anônimo que gastaria tokens de LLM.
        r = _client(authed=False).post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 401, r.text

    def test_409_when_aposentado(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe("aposentado")))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 409, r.text

    def test_400_when_no_message(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={})
        assert r.status_code == 400, r.text

    def test_404_when_pipeline_missing(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(None))
        r = _client().post("/api/v1/pipelines/ghost/invoke", json={"message": "oi"})
        assert r.status_code == 404, r.text

    def test_422_when_no_root(self, monkeypatch):
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": None, "nodes": [], "edges": []}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 422, r.text

    def test_happy_executes_sealed(self, monkeypatch):
        captured = {}
        async def fake_exec(**k):
            captured.update(k)
            return {"status": "completed", "output": "resposta", "final_state": "Recommend",
                    "interaction_id": "int1", "total_agents": 2, "completed_agents": 2,
                    "pipeline_steps": [{"agent_id": "r"}, {"agent_id": "a"}], "duration_ms": 42}
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({
            "root_agent_id": "r", "nodes": [{"id": "r"}, {"id": "a"}], "edges": [],
        }))
        monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi", "session_id": "s1"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pipeline_id"] == "p1"
        assert body["output"] == "resposta"
        assert body["completed_agents"] == 2
        # executou SELADO: allowed_agent_ids = membros do subgrafo; entry = raiz
        assert captured["entry_agent_id"] == "r"
        assert captured["allowed_agent_ids"] == {"r", "a"}
        assert captured["session_id"] == "s1"

    def _seed_happy(self, monkeypatch, captured):
        async def fake_exec(**k):
            captured.update(k)
            return {"status": "completed", "output": "ok", "interaction_id": "i1",
                    "pipeline_steps": [{"agent_id": "r"}], "total_agents": 1,
                    "completed_agents": 1, "duration_ms": 1}
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe()))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({
            "root_agent_id": "r", "nodes": [{"id": "r"}], "edges": [],
        }))
        monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))

    def test_context_mode_default_auto(self, monkeypatch):
        # API-1: ausente → 'auto' (comportamento atual: reinjeta memória da sessão).
        captured = {}
        self._seed_happy(monkeypatch, captured)
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 200, r.text
        assert captured["context_mode"] == "auto"

    def test_context_mode_none_threaded(self, monkeypatch):
        # API-1: 'none' chega ao execute_pipeline → invoke stateless/idempotente
        # (não reconstrói a janela da sessão mesmo com session_id).
        captured = {}
        self._seed_happy(monkeypatch, captured)
        r = _client().post(
            "/api/v1/pipelines/p1/invoke",
            json={"message": "oi", "session_id": "s1", "context_mode": "none"},
        )
        assert r.status_code == 200, r.text
        assert captured["context_mode"] == "none"
        assert captured["session_id"] == "s1"

    def test_aposentado_engine_valueerror_maps_409(self, monkeypatch):
        # Defesa em profundidade: se o gate do motor levantar ValueError, vira 409.
        monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(_pipe("publicado")))
        monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
        async def boom(**k):
            raise ValueError("Pipeline 'X' está aposentado — não é roteável.")
        monkeypatch.setattr(engine, "execute_pipeline", boom)
        monkeypatch.setattr(db.audit_repo, "create", _async({}))
        r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
        assert r.status_code == 409, r.text
