"""35.3.0 — lote de fast-follows dos jobs (#590/#584/#591).

- GET /pipelines/{pid}/jobs: listagem owner-scoped (recupera job_id perdido).
- sweep_pending do verifier_jobs: fila do juiz drena ENTRE boots (carona no
  reaper do invoke_jobs), sem resetar 'running' (≠ resume_jobs, que é só-boot).
- output_agent persistido no trace_data (engine + workspace) — acende quando
  o envelope tiver o campo (#591); None antes disso (aditivo).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_user
from app.routes.pipelines import router as pipelines_router


def _mk_client(user=None):
    app = FastAPI()
    app.include_router(pipelines_router)
    app.dependency_overrides[require_user] = lambda: (user or {"id": "u1", "role": "comum"})
    return TestClient(app)


def _job(jid, owner="u1", status="completed"):
    return {"id": jid, "pipeline_id": "p1", "owner_user_id": owner,
            "status": status, "attempts": 1, "idempotency_key": None,
            "created_at": None, "started_at": None, "finished_at": None,
            "result_payload": '{"output": "x"}', "error": None}


class TestListInvokeJobs:
    def _wire(self, monkeypatch, rows):
        find_all = AsyncMock(return_value=rows)
        monkeypatch.setattr("app.core.database.invoke_jobs_repo",
                            SimpleNamespace(find_all=find_all))
        return find_all

    def test_lista_escopada_ao_dono(self, monkeypatch):
        fa = self._wire(monkeypatch, [_job("j1"), _job("j2")])
        r = _mk_client().get("/api/v1/pipelines/p1/jobs")
        assert r.status_code == 200
        body = r.json()["jobs"]
        assert [j["job_id"] for j in body] == ["j1", "j2"]
        # envelope LEVE: sem result nem request_payload
        assert "result" not in body[0] and "request_payload" not in body[0]
        assert body[0]["status_url"].endswith("/jobs/j1")
        # filtro de posse aplicado na QUERY (não pós-filtro)
        assert fa.call_args.kwargs["owner_user_id"] == "u1"
        assert fa.call_args.kwargs["pipeline_id"] == "p1"

    def test_root_ve_todos_do_pipeline(self, monkeypatch):
        fa = self._wire(monkeypatch, [])
        r = _mk_client(user={"id": "adm", "role": "root"}).get("/api/v1/pipelines/p1/jobs")
        assert r.status_code == 200
        assert "owner_user_id" not in fa.call_args.kwargs

    def test_filtro_de_status(self, monkeypatch):
        fa = self._wire(monkeypatch, [])
        _mk_client().get("/api/v1/pipelines/p1/jobs?status=queued&limit=5")
        assert fa.call_args.kwargs["status"] == "queued"
        assert fa.call_args.kwargs["limit"] == 5

    def test_superficie_publica_para_keys(self):
        from app.core.api_auth import _is_public_surface
        assert _is_public_surface("GET", "/api/v1/pipelines/p1/jobs")


class TestVerifierSweep:
    def _pool(self, monkeypatch, rows):
        class _Con:
            def __init__(self):
                self.calls = []

            async def fetch(self, sql, *args):
                self.calls.append((sql, args))
                return rows
        con = _Con()

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr("app.core.database._get_pool",
                            lambda: SimpleNamespace(acquire=lambda: _Ctx()))
        return con

    @pytest.mark.asyncio
    async def test_sweep_nao_reseta_running_e_respeita_guarda_de_idade(self, monkeypatch):
        from app.verifier import async_dispatcher as d
        d._reset_for_tests()
        monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(
            verifier_job_max_attempts=3, verifier_max_concurrent_jobs=20))
        con = self._pool(monkeypatch, [])
        n = await d.sweep_pending(batch=10)
        assert n == 0
        sql = con.calls[0][0]
        # NUNCA reseta 'running' (≠ resume_jobs, que é só-boot/single-flight)
        assert "UPDATE" not in sql
        assert "status='pending'" in sql
        # guarda de idade: cobre a janela dispatch→_job_start de jobs recém-criados
        assert "'2 minutes'" in sql
        d._reset_for_tests()

    @pytest.mark.asyncio
    async def test_sweep_sem_slot_nao_consulta(self, monkeypatch):
        from app.verifier import async_dispatcher as d
        d._reset_for_tests()
        monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(
            verifier_job_max_attempts=3, verifier_max_concurrent_jobs=0))
        con = self._pool(monkeypatch, [])
        assert await d.sweep_pending() == 0
        assert con.calls == []  # cap 0 → nem toca o banco
        d._reset_for_tests()

    def test_reaper_chama_o_sweep(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert "from app.verifier.async_dispatcher import sweep_pending" in src
        assert "await sweep_pending()" in src


class TestOutputAgentNoTrace:
    def test_allowlists_persistem_output_agent(self):
        eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert '"output_agent": final_result.get("output_agent")' in eng
        ws = Path("app/routes/workspace.py").read_text(encoding="utf-8")
        assert '"output_agent"]}' in ws  # último item do allowlist do trace_persist
