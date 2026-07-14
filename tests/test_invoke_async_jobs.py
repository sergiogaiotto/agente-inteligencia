"""Onda 6 — invoke assíncrono 202 + job store durável (invoke_jobs, 34.0.0).

Cobre: o módulo app/core/invoke_jobs.py (create/claim/worker/resume/reaper/
shutdown, com pool fake), as rotas POST /invoke/async e GET /jobs/{id}
(gate do setting, dry rejeitado, 202+Location, Idempotency-Key replay/409,
posse IDOR no GET, projeção por auth do consultante) e a fiação (SCHEMA,
superfície pública p/ keys, lifespan).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import invoke_jobs
from app.core.auth import require_user
from app.routes.pipelines import router as pipelines_router


# ─────────────────────────────────────────────────────────────────────────────
# Infra de teste
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _estado_limpo():
    """Estado módulo-level (tasks/ids/reaper) vaza entre testes — lição do
    breaker #566."""
    invoke_jobs._reset_for_tests()
    yield
    invoke_jobs._reset_for_tests()


class FakeCon:
    def __init__(self, fetchrow_results=None, fetch_results=None, execute_results=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchrow = list(fetchrow_results or [])
        self._fetch = list(fetch_results or [])
        self._execute = list(execute_results or [])

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self._execute.pop(0) if self._execute else "UPDATE 1"

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchrow.pop(0) if self._fetchrow else None

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch.pop(0) if self._fetch else []

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


def _use_pool(monkeypatch, con: FakeCon):
    monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))


def _settings_stub(monkeypatch, **overrides):
    base = dict(invoke_async_enabled=True, invoke_jobs_retention_hours=72,
                invoke_jobs_max_concurrent=4, api_key_invoke_published_only=False)
    base.update(overrides)
    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(**base))


# ─────────────────────────────────────────────────────────────────────────────
# Módulo: create_job / dispatch / worker
# ─────────────────────────────────────────────────────────────────────────────

class TestCreateJob:
    @pytest.mark.asyncio
    async def test_insert_devolve_created_true(self, monkeypatch):
        row = {"id": "ij_novo", "pipeline_id": "p1", "status": "queued"}
        con = FakeCon(fetchrow_results=[row])
        _use_pool(monkeypatch, con)
        job, created = await invoke_jobs.create_job(
            pipeline_id="p1", owner_user_id="u1", api_key_id=None,
            idempotency_key="k-1", request_payload={"root": "a1"})
        assert created is True and job["id"] == "ij_novo"
        sql, args = con.calls[0]
        assert "ON CONFLICT DO NOTHING" in sql
        assert args[1] == "p1" and args[4] == "k-1"
        assert json.loads(args[5]) == {"root": "a1"}  # TEXT + json.dumps (footgun asyncpg)

    @pytest.mark.asyncio
    async def test_replay_idempotency_devolve_existente(self, monkeypatch):
        existente = {"id": "ij_velho", "pipeline_id": "p1", "status": "running"}
        con = FakeCon(fetchrow_results=[None, existente])  # INSERT conflitou → SELECT
        _use_pool(monkeypatch, con)
        job, created = await invoke_jobs.create_job(
            pipeline_id="p1", owner_user_id="u1", api_key_id="k-abc",
            idempotency_key="k-1", request_payload={})
        assert created is False and job["id"] == "ij_velho"
        # lookup escopado POR key-criadora (integrações irmãs não colidem)
        sel_sql, sel_args = con.calls[1]
        assert "COALESCE(api_key_id, '') = COALESCE($4, '')" in sel_sql
        assert sel_args[3] == "k-abc"

    @pytest.mark.asyncio
    async def test_find_existing_sem_key_e_noop(self, monkeypatch):
        con = FakeCon()
        _use_pool(monkeypatch, con)
        assert await invoke_jobs.find_existing_job(
            owner_user_id="u1", api_key_id=None, pipeline_id="p1",
            idempotency_key=None) is None
        assert con.calls == []  # nem toca o banco


class TestDispatch:
    @pytest.mark.asyncio
    async def test_respeita_cap_e_duplicata(self, monkeypatch):
        _settings_stub(monkeypatch, invoke_jobs_max_concurrent=1)

        async def _noop(job_id):
            await asyncio.sleep(0.05)
        monkeypatch.setattr(invoke_jobs, "_run_job", _noop)

        assert invoke_jobs.dispatch("j1") is True
        assert invoke_jobs.dispatch("j1") is False  # já em voo
        assert invoke_jobs.dispatch("j2") is False  # cap=1
        await asyncio.sleep(0.1)
        assert invoke_jobs.dispatch("j2") is True   # vaga abriu


class TestRunJob:
    def _claimed(self, req_overrides=None, job_overrides=None):
        req = {"root": "a1", "members": ["a1", "a2"], "user_input": "oi",
               "channel": "api", "context_mode": "auto", "arg_keys": [],
               "request_id": "rid-1"}
        req.update(req_overrides or {})
        job = {"id": "j1", "pipeline_id": "p1", "owner_user_id": "u1",
               "request_payload": json.dumps(req)}
        job.update(job_overrides or {})
        return job

    def _wire(self, monkeypatch, con, *, pipeline=None, engine=None):
        _use_pool(monkeypatch, con)
        _settings_stub(monkeypatch)
        from app.core import database as db
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value=pipeline or {"id": "p1", "status": "publicado"}))
        eng = engine or AsyncMock(return_value={
            "status": "completed", "output": "resposta", "final_state": "Done",
            "interaction_id": "i-9", "total_agents": 2, "completed_agents": 2,
            "pipeline_steps": [], "duration_ms": 42.0})
        monkeypatch.setattr("app.agents.engine.execute_pipeline", eng)
        stamp = AsyncMock()
        monkeypatch.setattr("app.core.interaction_access.stamp_interaction_owner", stamp)
        rec = MagicMock(side_effect=lambda **kw: _closed_coro())
        monkeypatch.setattr("app.routes.pipelines._record_invoke_analytics", rec)
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics",
                            lambda coro: coro.close())
        return eng, stamp, rec

    @pytest.mark.asyncio
    async def test_claim_perdido_e_noop(self, monkeypatch):
        con = FakeCon(fetchrow_results=[None])  # já claimado por outro caminho
        _use_pool(monkeypatch, con)
        await invoke_jobs._run_job("j1")
        assert len(con.calls) == 1  # só o claim

    @pytest.mark.asyncio
    async def test_caminho_feliz_persiste_completed_e_analytics_async(self, monkeypatch):
        con = FakeCon(fetchrow_results=[self._claimed()])
        eng, stamp, rec = self._wire(monkeypatch, con)
        await invoke_jobs._run_job("j1")
        # engine recebeu a cadeia SELADA do payload persistido
        kw = eng.call_args.kwargs
        assert kw["allowed_agent_ids"] == {"a1", "a2"} and kw["pipeline_id"] == "p1"
        # posse carimbada ANTES do polling ver o interaction_id
        stamp.assert_awaited_once_with("i-9", "u1")
        done = con.sql_containing("status='completed'")
        assert done, "worker não persistiu completed"
        payload = json.loads(done[0][1][1])
        assert payload["output"] == "resposta" and payload["pipeline_id"] == "p1"
        assert rec.call_args.kwargs["kind"] == "invoke_async"

    @pytest.mark.asyncio
    async def test_valueerror_vira_rejected_com_detalhe(self, monkeypatch):
        con = FakeCon(fetchrow_results=[self._claimed()])
        self._wire(monkeypatch, con, engine=AsyncMock(side_effect=ValueError("sem raiz")))
        await invoke_jobs._run_job("j1")
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "pipeline_execution_rejected" and "sem raiz" in err["detail"]

    @pytest.mark.asyncio
    async def test_excecao_generica_nao_vaza_str_e(self, monkeypatch):
        con = FakeCon(fetchrow_results=[self._claimed()])
        self._wire(monkeypatch, con,
                   engine=AsyncMock(side_effect=RuntimeError("segredo-interno-boom")))
        await invoke_jobs._run_job("j1")
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "pipeline_execution_failed"
        assert err.get("request_id") == "rid-1"
        assert "segredo-interno-boom" not in json.dumps(err)

    @pytest.mark.asyncio
    async def test_recheck_aposentado_falha_sem_executar(self, monkeypatch):
        con = FakeCon(fetchrow_results=[self._claimed()])
        eng, _, _ = self._wire(monkeypatch, con,
                               pipeline={"id": "p1", "status": "aposentado"})
        await invoke_jobs._run_job("j1")
        eng.assert_not_awaited()
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "pipeline_not_invocable"

    @pytest.mark.asyncio
    async def test_recheck_toctou_sessao_de_outro_dono_falha(self, monkeypatch):
        """Review: interaction legada-sem-dono pode GANHAR dono entre o aceite e
        a execução — o worker não injeta a conversa alheia no LLM."""
        con = FakeCon(fetchrow_results=[self._claimed({"session_id": "s-1"})])
        eng, _, _ = self._wire(monkeypatch, con)
        monkeypatch.setattr("app.core.interaction_access.owner_of_interaction",
                            AsyncMock(return_value="OUTRO-USUARIO"))
        await invoke_jobs._run_job("j1")
        eng.assert_not_awaited()
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "session_not_accessible"

    @pytest.mark.asyncio
    async def test_recheck_key_revogada_falha_sem_executar(self, monkeypatch):
        con = FakeCon(fetchrow_results=[
            self._claimed({"api_key_id": "k-1"}),
            {"revoked_at": "2026-07-13"},  # SELECT revoked_at da key
        ])
        eng, _, _ = self._wire(monkeypatch, con)
        await invoke_jobs._run_job("j1")
        eng.assert_not_awaited()
        err = json.loads(con.sql_containing("status='failed'")[0][1][1])
        assert err["error"] == "api_key_revoked"


def _closed_coro():
    async def _c():
        pass
    return _c()


# ─────────────────────────────────────────────────────────────────────────────
# Módulo: resume / reaper / shutdown
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeEReaper:
    @pytest.mark.asyncio
    async def test_boot_running_orfao_vira_lost_e_queued_retoma(self, monkeypatch):
        # 35.6.0: o UPDATE→lost virou fetch(RETURNING) p/ notificar webhooks —
        # 1º fetch = lost rows; 2º = queued.
        lost_rows = [
            {"id": "l1", "pipeline_id": "p1",
             "request_payload": json.dumps({"webhook_url": "https://x.example.com/h"})},
            {"id": "l2", "pipeline_id": "p1", "request_payload": "{}"},
        ]
        con = FakeCon(fetch_results=[lost_rows, [{"id": "q1"}, {"id": "q2"}]])
        _use_pool(monkeypatch, con)
        _settings_stub(monkeypatch)
        despachados = []
        monkeypatch.setattr(invoke_jobs, "dispatch", lambda j: despachados.append(j) or True)
        notificados = []
        monkeypatch.setattr(invoke_jobs, "_notify_finish",
                            lambda jid, pid, req, status, code=None: notificados.append((jid, status)))
        out = await invoke_jobs.resume_invoke_jobs()
        assert out == {"lost": 2, "dispatched": 2} and despachados == ["q1", "q2"]
        lost_sql = con.sql_containing("SET status='lost'")[0]
        assert "WHERE status='running'" in lost_sql[0] and "RETURNING" in lost_sql[0]
        assert json.loads(lost_sql[1][0])["error"] == "job_interrupted"
        # webhook do lost notificado (o _notify_finish decide pelo payload)
        assert ("l1", "lost") in notificados and ("l2", "lost") in notificados

    @pytest.mark.asyncio
    async def test_kill_switch_congela_fila_mas_mantem_higiene(self, monkeypatch):
        """Review (major): desligar invoke_async_enabled tem que parar o BACKLOG
        (nada novo paga LLM), não só os 202 novos — mas a higiene (running órfão
        → lost, retenção) continua."""
        con = FakeCon(fetch_results=[[{"id": "l1", "pipeline_id": "p1", "request_payload": "{}"}]])
        _use_pool(monkeypatch, con)
        _settings_stub(monkeypatch, invoke_async_enabled=False)
        despachou = MagicMock()
        monkeypatch.setattr(invoke_jobs, "dispatch", despachou)
        out = await invoke_jobs.resume_invoke_jobs()
        assert out["lost"] == 1 and out["dispatched"] == 0
        despachou.assert_not_called()
        assert not con.sql_containing("status='queued' ORDER BY")  # nem consulta a fila

        con2 = FakeCon(execute_results=["DELETE 5"], fetch_results=[[]])
        _use_pool(monkeypatch, con2)
        out2 = await invoke_jobs.reap_once()
        assert out2["deleted"] == 5 and out2["dispatched"] == 0
        despachou.assert_not_called()

    @pytest.mark.asyncio
    async def test_reap_retencao_zumbi_e_fila(self, monkeypatch):
        con = FakeCon(
            execute_results=["DELETE 3", "UPDATE 1"],
            fetch_results=[[{"id": "z-vivo"}, {"id": "z-morto"}], [{"id": "q1"}]],
        )
        _use_pool(monkeypatch, con)
        _settings_stub(monkeypatch, invoke_jobs_retention_hours=48)
        invoke_jobs._active_job_ids.add("z-vivo")  # tem task viva → NÃO é zumbi
        monkeypatch.setattr(invoke_jobs, "dispatch", lambda j: True)
        out = await invoke_jobs.reap_once()
        assert out == {"deleted": 3, "lost": 1, "dispatched": 1}
        del_sql, del_args = con.sql_containing("DELETE FROM invoke_jobs")[0]
        assert "'completed','failed','lost'" in del_sql and del_args[0] == 48.0
        zombie_sql, zombie_args = con.sql_containing("id = ANY($1)")[0]
        assert zombie_args[0] == ["z-morto"]

    @pytest.mark.asyncio
    async def test_shutdown_cancela_reaper_e_marca_lost(self, monkeypatch):
        con = FakeCon()
        _use_pool(monkeypatch, con)
        invoke_jobs.start_reaper()
        assert invoke_jobs._reaper_task is not None

        async def _pendura():
            await asyncio.sleep(30)
        t = asyncio.create_task(_pendura())
        invoke_jobs._active_tasks.add(t)
        invoke_jobs._active_job_ids.add("j-preso")
        await invoke_jobs.shutdown_invoke_jobs(timeout=0.05)
        assert invoke_jobs._reaper_task is None
        marked = con.sql_containing("SET status='lost'")
        assert marked and marked[0][1][0] == ["j-preso"]


# ─────────────────────────────────────────────────────────────────────────────
# Rotas
# ─────────────────────────────────────────────────────────────────────────────

def _mk_client(user=None):
    app = FastAPI()
    app.include_router(pipelines_router)
    app.dependency_overrides[require_user] = lambda: (user or {"id": "u1", "role": "comum"})
    return TestClient(app)


def _wire_post(monkeypatch, *, created=True, existing_payload=None):
    _settings_stub(monkeypatch)
    from app.core import database as db
    monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                        AsyncMock(return_value={"id": "p1", "status": "publicado", "name": "P"}))
    monkeypatch.setattr(
        "app.catalog.pipeline_defs._build_subgraph",
        AsyncMock(return_value={"root_agent_id": "a1", "nodes": [{"id": "a1"}, {"id": "a2"}]}))

    async def _create_job(**kw):
        payload = existing_payload if existing_payload is not None else kw["request_payload"]
        job = {"id": "ij_teste", "pipeline_id": kw["pipeline_id"], "status": "queued",
               "attempts": 0, "idempotency_key": kw["idempotency_key"],
               "request_payload": json.dumps(payload)}
        return job, created
    cj = AsyncMock(side_effect=_create_job)
    monkeypatch.setattr("app.core.invoke_jobs.create_job", cj)
    # lookup precoce do replay: default "não achou" (fluxo de criação segue)
    monkeypatch.setattr("app.core.invoke_jobs.find_existing_job",
                        AsyncMock(return_value=None))
    disp = MagicMock(return_value=True)
    monkeypatch.setattr("app.core.invoke_jobs.dispatch", disp)
    return cj, disp


def _fingerprint_de(pid, **body):
    from app.models.schemas import PipelineInvokeRequest
    from app.routes.pipelines import _request_fingerprint
    return _request_fingerprint(pid, PipelineInvokeRequest(**body))


class TestPostInvokeAsync:
    def test_gated_off_403(self, monkeypatch):
        _settings_stub(monkeypatch, invoke_async_enabled=False)
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async", json={"message": "oi"})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "invoke_async_disabled"

    def test_dry_rejeitado_400(self, monkeypatch):
        _settings_stub(monkeypatch)
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async",
                              json={"message": "oi", "dry": True})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "dry_not_supported_async"

    def test_202_com_location_e_job(self, monkeypatch):
        cj, disp = _wire_post(monkeypatch)
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async", json={"message": "oi"})
        assert r.status_code == 202
        body = r.json()
        assert body["job_id"] == "ij_teste" and body["status"] == "queued"
        assert body["job_schema_version"] == "1"
        assert r.headers["location"] == "/api/v1/pipelines/p1/jobs/ij_teste"
        assert r.headers["retry-after"] == "2"
        # contexto persistido como VALORES: cadeia selada + dono
        kw = cj.call_args.kwargs
        assert kw["owner_user_id"] == "u1"
        assert kw["request_payload"]["members"] == ["a1", "a2"]
        assert kw["request_payload"]["root"] == "a1"
        disp.assert_called_once_with("ij_teste")

    def test_replay_mesmo_corpo_200_mesmo_job(self, monkeypatch):
        _, disp = _wire_post(monkeypatch, created=False)  # payload eco = mesmo hash
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async",
                              json={"message": "oi"},
                              headers={"Idempotency-Key": "op-1"})
        assert r.status_code == 200
        assert r.json()["job_id"] == "ij_teste"
        disp.assert_not_called()  # replay NÃO re-executa (não paga LLM de novo)

    def test_replay_corpo_diferente_409(self, monkeypatch):
        _wire_post(monkeypatch, created=False,
                   existing_payload={"request_hash": "OUTRO-HASH"})
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async",
                              json={"message": "oi"},
                              headers={"Idempotency-Key": "op-1"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "idempotency_key_reuse"

    def test_replay_precoce_sobrevive_a_gate_mutavel(self, monkeypatch):
        """Review (major): o retry do proxy com a MESMA Idempotency-Key tem que
        recuperar o job mesmo que o orçamento tenha estourado / o pipeline tenha
        sido aposentado DEPOIS do aceite — o lookup roda ANTES dos gates. Aqui
        os gates explodiriam (repo não mockado); só o replay-precoce salva."""
        _settings_stub(monkeypatch)
        job = {"id": "ij_pago", "pipeline_id": "p1", "status": "completed",
               "attempts": 1, "idempotency_key": "op-1",
               "request_payload": json.dumps(
                   {"request_hash": _fingerprint_de("p1", message="oi")})}
        monkeypatch.setattr("app.core.invoke_jobs.find_existing_job",
                            AsyncMock(return_value=job))
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async",
                              json={"message": "oi"},
                              headers={"Idempotency-Key": "op-1"})
        assert r.status_code == 200
        assert r.json()["job_id"] == "ij_pago"
        assert r.headers["location"].endswith("/jobs/ij_pago")

    def test_replay_precoce_corpo_diferente_409(self, monkeypatch):
        _settings_stub(monkeypatch)
        job = {"id": "ij_pago", "pipeline_id": "p1", "status": "completed",
               "attempts": 1, "idempotency_key": "op-1",
               "request_payload": json.dumps({"request_hash": "OUTRO"})}
        monkeypatch.setattr("app.core.invoke_jobs.find_existing_job",
                            AsyncMock(return_value=job))
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async",
                              json={"message": "oi"},
                              headers={"Idempotency-Key": "op-1"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"] == "idempotency_key_reuse"

    def test_aposentado_409_no_aceite(self, monkeypatch):
        _wire_post(monkeypatch)
        from app.core import database as db
        monkeypatch.setattr(db.pipelines_repo, "find_by_id",
                            AsyncMock(return_value={"id": "p1", "status": "aposentado", "name": "P"}))
        r = _mk_client().post("/api/v1/pipelines/p1/invoke/async", json={"message": "oi"})
        assert r.status_code == 409


class TestGetInvokeJob:
    def _job(self, **over):
        base = {"id": "j1", "pipeline_id": "p1", "owner_user_id": "u1",
                "status": "queued", "attempts": 0, "idempotency_key": None,
                "created_at": None, "started_at": None, "finished_at": None,
                "result_payload": None, "error": None}
        base.update(over)
        return base

    def _wire(self, monkeypatch, job):
        monkeypatch.setattr("app.core.database.invoke_jobs_repo",
                            SimpleNamespace(find_by_id=AsyncMock(return_value=job)))

    def test_dono_ve_o_job(self, monkeypatch):
        self._wire(monkeypatch, self._job())
        r = _mk_client().get("/api/v1/pipelines/p1/jobs/j1")
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert "result" not in r.json()

    def test_alheio_404_identico(self, monkeypatch):
        self._wire(monkeypatch, self._job(owner_user_id="OUTRO"))
        r = _mk_client().get("/api/v1/pipelines/p1/jobs/j1")
        assert r.status_code == 404 and r.json()["detail"] == "Job não encontrado"

    def test_root_bypass(self, monkeypatch):
        self._wire(monkeypatch, self._job(owner_user_id="OUTRO"))
        r = _mk_client(user={"id": "adm", "role": "root"}).get("/api/v1/pipelines/p1/jobs/j1")
        assert r.status_code == 200

    def test_pid_errado_404(self, monkeypatch):
        self._wire(monkeypatch, self._job(pipeline_id="p2"))
        r = _mk_client().get("/api/v1/pipelines/p1/jobs/j1")
        assert r.status_code == 404

    def test_completed_projeta_result_full_para_cookie(self, monkeypatch):
        payload = {"pipeline_id": "p1", "status": "completed", "output": "resposta",
                   "final_state": "Done", "interaction_id": "i-9", "total_agents": 2,
                   "completed_agents": 2, "pipeline_steps": [], "duration_ms": 42.0}
        self._wire(monkeypatch, self._job(status="completed",
                                          result_payload=json.dumps(payload)))
        r = _mk_client().get("/api/v1/pipelines/p1/jobs/j1")
        res = r.json()["result"]
        assert res["schema_version"] == "1" and res["verbosity"] == "full"
        assert res["output"] == "resposta" and res["pipeline_steps"] == []

    def test_failed_expoe_erro_estruturado(self, monkeypatch):
        self._wire(monkeypatch, self._job(
            status="failed", error=json.dumps({"error": "pipeline_execution_failed"})))
        r = _mk_client().get("/api/v1/pipelines/p1/jobs/j1")
        assert r.json()["error"]["error"] == "pipeline_execution_failed"


# ─────────────────────────────────────────────────────────────────────────────
# Fiação (SCHEMA / superfície pública / lifespan)
# ─────────────────────────────────────────────────────────────────────────────

class TestFiacao:
    def test_tabela_no_schema_com_unique_parcial(self):
        from app.core.database import SCHEMA
        assert "CREATE TABLE IF NOT EXISTS invoke_jobs" in SCHEMA
        assert "uq_invoke_jobs_idem" in SCHEMA
        assert "WHERE idempotency_key IS NOT NULL" in SCHEMA
        # idempotência POR key-criadora (integrações irmãs não colidem)
        assert "(COALESCE(api_key_id, ''))" in SCHEMA

    def test_stream_ganhou_os_gates_compartilhados(self):
        """Review (major): o /invoke/stream tinha divergido do sync (sem escopo
        de key, sem IDOR do session_id, sem stamp de posse). As 3 rotas agora
        consomem os MESMOS helpers, e o stream carimba o dono."""
        src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert src.count("await _resolve_invoke_target(pid, data, request)") == 3
        assert src.count("await _finalize_invoke_inputs(") == 3
        # stamp dentro do _run() do stream (result or {})
        assert 'await stamp_interaction_owner((result or {}).get("interaction_id"), user.get("id"))' in src

    def test_superficie_publica_aceita_invoke_async(self):
        from app.core.api_auth import _is_public_surface
        assert _is_public_surface("POST", "/api/v1/pipelines/p1/invoke/async")
        assert _is_public_surface("GET", "/api/v1/pipelines/p1/jobs/j1")
        assert not _is_public_surface("POST", "/api/v1/pipelines/p1/jobs/j1")

    def test_lifespan_fia_resume_reaper_e_shutdown(self):
        src = Path("app/main.py").read_text(encoding="utf-8")
        assert "resume_invoke_jobs" in src and "start_reaper" in src
        assert "shutdown_invoke_jobs" in src

    def test_rate_bucket_do_polling_nao_e_workspace(self):
        # POST async herda o bucket caro (dispara LLM); o GET de polling não.
        from app.core.ratelimit import _bucket_for_path
        assert _bucket_for_path("/api/v1/pipelines/p1/invoke/async")[0] == "workspace"
        assert _bucket_for_path("/api/v1/pipelines/p1/jobs/j1")[0] == "api"
