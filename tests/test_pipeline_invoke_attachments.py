"""Anexos no invoke de pipeline (fatia #3 da experiência de execução v2).

O modal "Executar" passou a aceitar arquivos. O backend mapeia a saída do
/workspace/upload pra forma que o engine consome ({name,type,size,content,
abs_path}) e passa em `attachments` ao execute_pipeline — que roteia cada anexo
só aos agentes da cadeia que aceitam doc/imagem (dispatcher). Sem anexo → None.
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as db
import app.catalog.pipeline_defs as pdefs
import app.agents.engine as engine
from app.routes import pipelines as pl_routes
from app.models.schemas import PipelineInvokeRequest

MESH_FLOW = Path("app/templates/pages/mesh_flow.html")


def _async(value):
    async def _fn(*a, **k):
        return value
    return _fn


def _client():
    app = FastAPI()
    app.include_router(pl_routes.router)
    app.dependency_overrides[pl_routes.require_user] = lambda: {"id": "u-test"}
    return TestClient(app, raise_server_exceptions=False)


def _stub(monkeypatch, capture):
    async def fake_exec(**k):
        capture.update(k)
        return {"status": "completed", "output": "ok", "pipeline_steps": [],
                "total_agents": 1, "completed_agents": 1, "interaction_id": "i", "duration_ms": 1}
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async({"id": "p1", "name": "P", "status": "publicado"}))
    monkeypatch.setattr(pdefs, "_build_subgraph", _async({"root_agent_id": "r", "nodes": [{"id": "r"}], "edges": []}))
    monkeypatch.setattr(engine, "execute_pipeline", fake_exec)
    monkeypatch.setattr(db.audit_repo, "create", _async({}))


def test_schema_aceita_attachments():
    m = PipelineInvokeRequest(message="oi", attachments=[{"filename": "a.pdf"}])
    assert m.attachments == [{"filename": "a.pdf"}]
    assert PipelineInvokeRequest(message="oi").attachments is None


def test_invoke_mapeia_e_encaminha_anexo(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={
        "message": "analise",
        "attachments": [{"filename": "a.pdf", "content_type": "application/pdf",
                         "size": 10, "text_content": "texto extraído", "path": "abc_a.pdf"}],
    })
    assert r.status_code == 200, r.text
    atts = cap["attachments"]
    assert len(atts) == 1
    a = atts[0]
    assert a["name"] == "a.pdf" and a["type"] == "application/pdf"
    assert a["content"] == "texto extraído" and a["size"] == 10
    assert a["abs_path"].endswith("abc_a.pdf")  # basename saneado sob UPLOAD_DIR


def test_invoke_sem_anexo_passa_none(monkeypatch):
    cap = {}
    _stub(monkeypatch, cap)
    r = _client().post("/api/v1/pipelines/p1/invoke", json={"message": "oi"})
    assert r.status_code == 200, r.text
    assert cap["attachments"] is None


def test_modal_tem_uploader():
    src = MESH_FLOW.read_text(encoding="utf-8")
    assert 'data-testid="pipeline-run-attach"' in src
    assert "uploadRunFiles(" in src
    assert "/api/v1/workspace/upload" in src
    # envia os anexos no invoke + estado inicial no modal
    assert "attachments: this.runModal.attachments" in src
    assert "attachments: [], uploading: false" in src
