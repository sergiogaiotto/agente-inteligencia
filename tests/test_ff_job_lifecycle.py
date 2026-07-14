"""FF4 (35.4.0) — ciclo de vida do invoke-job: deadline + escopo de LEITURA.

1) Deadline por job (`invoke_job_timeout_minutes`): um execute_pipeline
   pendurado ocupava vaga do cap PARA SEMPRE (o reaper de propósito não mata
   'running' com task viva). Agora wait_for cancela no estouro e o job vira
   failed(job_timeout) — erro NOMEADO antes do except Exception genérico.

2) Escopo de leitura por-key (tema #583): key escopada a [P1] não lê jobs de
   P2 (nem os do próprio dono) — o escopo contém o raio de uma key vazada.
   read_only PODE ler (é o propósito dela); só allowed_pipeline_ids restringe.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core import invoke_jobs
from app.core.apikey_scope import assert_api_key_can_read_pipeline


@pytest.fixture(autouse=True)
def _estado_limpo():
    invoke_jobs._reset_for_tests()
    yield
    invoke_jobs._reset_for_tests()


class FakeCon:
    def __init__(self, fetchrow_results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchrow = list(fetchrow_results or [])

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return []

    def sql_containing(self, frag):
        return [c for c in self.calls if frag in c[0]]


class FakePool:
    def __init__(self, con):
        self._con = con

    def acquire(self):
        con = self._con

        class _Ctx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def _settings_stub(monkeypatch, **overrides):
    base = dict(invoke_async_enabled=True, invoke_jobs_retention_hours=72,
                invoke_jobs_max_concurrent=4, invoke_job_timeout_minutes=30)
    base.update(overrides)
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(**base))


class TestDeadlinePorJob:
    def _claimed(self):
        return {"id": "j1", "pipeline_id": "p1", "owner_user_id": "u1",
                "request_payload": json.dumps({"root": "a1", "members": ["a1"],
                                               "user_input": "oi", "arg_keys": [],
                                               "request_id": "rid-1"})}

    @pytest.mark.asyncio
    async def test_engine_pendurado_vira_job_timeout(self, monkeypatch):
        """Timeout: cancela a execução, nomeia o erro, PASSA o dono à criação e
        NÃO some com o custo dos steps já concluídos (review do FF4)."""
        con = FakeCon(fetchrow_results=[self._claimed()])
        monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))
        # 0.002 min = 0.12s — o engine "pendura" por 10s e é CANCELADO antes
        _settings_stub(monkeypatch, invoke_job_timeout_minutes=0.002)
        from app.core import database as db
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value={"id": "p1", "status": "publicado"}))
        visto = {"cancelado": False, "owner_kwarg": None}

        async def pendura(**kw):
            visto["owner_kwarg"] = kw.get("owner_user_id")
            # step 1 concluiu (gastou LLM de verdade) ANTES de pendurar
            await kw["progress_callback"]({
                "type": "agent_done", "agent_id": "a1", "agent_name": "A",
                "cost_usd": 0.5, "tokens_used": 1234, "duration_ms": 80,
                "interaction_id": "i-master",
            })
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                visto["cancelado"] = True
                raise
        monkeypatch.setattr("app.agents.engine.execute_pipeline", pendura)
        from unittest.mock import MagicMock
        rec = MagicMock(side_effect=lambda **kw: _closed_coro())
        monkeypatch.setattr("app.routes.pipelines._record_invoke_analytics", rec)
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics",
                            lambda coro: coro.close())

        await asyncio.wait_for(invoke_jobs._run_job("j1"), timeout=5)

        assert visto["cancelado"] is True          # a execução foi mesmo cancelada
        assert visto["owner_kwarg"] == "u1"        # dono flui até a CRIAÇÃO (IDOR)
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "job_timeout" and "timeout_minutes" in err
        # custo dos steps concluídos NÃO some: analytics com result sintético
        kw = rec.call_args.kwargs
        assert kw["kind"] == "invoke_async"
        assert kw["result"]["final_state"] == "JobTimeout"
        assert kw["result"]["pipeline_steps"][0]["cost_usd"] == 0.5
        assert kw["result"]["interaction_id"] == "i-master"

    @pytest.mark.asyncio
    async def test_execucao_dentro_do_deadline_completa_normal(self, monkeypatch):
        con = FakeCon(fetchrow_results=[self._claimed()])
        monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))
        _settings_stub(monkeypatch, invoke_job_timeout_minutes=30)
        from app.core import database as db
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value={"id": "p1", "status": "publicado"}))
        monkeypatch.setattr("app.agents.engine.execute_pipeline", AsyncMock(return_value={
            "status": "completed", "output": "ok", "final_state": "Done",
            "output_agent": {"id": "a1", "name": "A"},
            "interaction_id": "i1", "total_agents": 1, "completed_agents": 1,
            "pipeline_steps": [], "duration_ms": 5.0}))
        monkeypatch.setattr("app.core.interaction_access.stamp_interaction_owner", AsyncMock())
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics",
                            lambda coro: coro.close())
        await invoke_jobs._run_job("j1")
        assert con.sql_containing("status='completed'")
        assert not con.sql_containing("status='failed'")


class TestEscopoDeLeitura:
    def _req(self, scope):
        return SimpleNamespace(state=SimpleNamespace(api_key_scope=scope, api_key_id="k1"))

    def test_cookie_sem_escopo_passa(self):
        assert_api_key_can_read_pipeline(
            SimpleNamespace(state=SimpleNamespace(api_key_scope=None)), pipeline_id="p1")

    def test_read_only_PODE_ler(self):
        # ≠ do gate de invoke: leitura é o propósito de uma key read_only
        assert_api_key_can_read_pipeline(
            self._req({"read_only": True, "allowed_pipeline_ids": None}), pipeline_id="p1")

    def test_pipeline_fora_da_lista_403(self):
        with pytest.raises(HTTPException) as ei:
            assert_api_key_can_read_pipeline(
                self._req({"allowed_pipeline_ids": '["p2"]'}), pipeline_id="p1")
        assert ei.value.status_code == 403

    def test_pipeline_na_lista_passa(self):
        assert_api_key_can_read_pipeline(
            self._req({"allowed_pipeline_ids": '["p1","p2"]'}), pipeline_id="p1")

    def test_lista_vazia_libera_todos(self):
        assert_api_key_can_read_pipeline(
            self._req({"allowed_pipeline_ids": None}), pipeline_id="qualquer")


def _closed_coro():
    async def _c():
        pass
    return _c()


class TestDonoNaCriacao:
    """Review do FF4 (major): o timeout era o 1º aborto DETERMINÍSTICO depois
    da criação da interaction — sem dono na criação, master/filhas ficavam
    órfãs (listáveis por todos + sequestráveis no reuso do session_id)."""

    def test_contextvar_roundtrip(self):
        from app.core.interaction_access import (
            set_interaction_owner_for_creation, interaction_owner_for_creation)
        set_interaction_owner_for_creation("u-9")
        assert interaction_owner_for_creation() == "u-9"
        set_interaction_owner_for_creation("  ")   # vazio normaliza p/ None
        assert interaction_owner_for_creation() is None

    @pytest.mark.asyncio
    async def test_execute_pipeline_seta_o_contexto(self, monkeypatch):
        """O execute_pipeline REAL seta o dono antes do 1º step — visível
        dentro do execute_interaction (onde a criação acontece)."""
        import app.agents.engine as eng
        from app.core.interaction_access import interaction_owner_for_creation

        async def fake_agents(aid):
            return {"id": aid, "name": aid, "status": "active", "kind": "subagent",
                    "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "p"}
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agents)

        async def fake_mesh(source_agent_id=None, limit=20, **_):
            return []
        monkeypatch.setattr("app.core.database.mesh_repo.find_all", fake_mesh)
        visto = {}

        async def fake_exec(**kw):
            visto["owner_no_contexto"] = interaction_owner_for_creation()
            return {"output": "ok", "final_state": "Done", "interaction_id": None,
                    "duration_ms": 1, "evidence_score": 0, "transitions": [], "trace": {}}
        monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)

        await eng.execute_pipeline(entry_agent_id="T", user_input="oi",
                                   owner_user_id="dono-123")
        assert visto["owner_no_contexto"] == "dono-123"

    def test_pontos_de_criacao_incluem_o_dono(self):
        fsm = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
        assert 'interaction_owner_for_creation' in fsm
        assert '**({"owner_user_id": _owner} if _owner else {})' in fsm
        eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
        assert eng.count('**({"owner_user_id": _owner} if _owner else {})') == 1

    def test_rotas_e_worker_passam_o_dono(self):
        rotas = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        # as 2 chamadas de execute_pipeline (sync + stream) — distintas das do
        # job-store (create_job/find_existing_job), que já carregavam o dono
        assert rotas.count('owner_user_id=user.get("id"),  # 35.4.0') == 2
        jobs = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert 'owner_user_id=job.get("owner_user_id")' in jobs


class TestFiacao:
    def test_rotas_de_jobs_aplicam_escopo_de_leitura(self):
        src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        # as DUAS rotas (lista + detalhe) gateiam a leitura por-key
        assert src.count("assert_api_key_can_read_pipeline(request, pipeline_id=pid)") == 2

    def test_worker_tem_deadline_antes_do_except_generico(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert "asyncio.wait_for(" in src
        # TimeoutError herda de Exception — o handler nomeado PRECISA vir antes
        assert src.index("except (TimeoutError, asyncio.TimeoutError)") < src.index(
            'await _finish_failed(job_id, {"error": "pipeline_execution_rejected"')
        assert '"error": "job_timeout"' in src
