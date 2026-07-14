"""Resíduos da auditoria adversarial fresca — 3 minors (35.14.4).

M1: worker async descartava o custo REAL dos steps concluídos em erro/ValueError
    (assimétrico com o timeout, que já o preservava). → _schedule_partial_cost
    compartilhado pelos 3 aborts.
M2: ContextVars owner/customer setados SÓ quando truthy e nunca resetados →
    num loop sequencial na MESMA task, a operação N+1 herdava o titular da N.
    → set incondicional (None limpa).
M3: caronas do reaper (sweep/purga) sem teto de latência → uma lenta atrasava
    o despacho de jobs. → asyncio.wait_for por carona.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestM1CustoParcialNoAborto:
    def test_helper_agenda_result_sintetico_com_steps(self, monkeypatch):
        import app.core.invoke_jobs as ij
        agendado = {}

        def _cap(coro):
            agendado["frame"] = coro.cr_frame.f_locals
            coro.close()
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics", _cap)
        rec = MagicMock(side_effect=lambda **kw: _closed())
        monkeypatch.setattr("app.routes.pipelines._record_invoke_analytics", rec)
        job = {"id": "j1", "pipeline_id": "p1", "owner_user_id": "u1"}
        req = {"root": "a1", "members": ["a1", "a2"]}
        done = [{"agent_id": "a1", "cost_usd": 0.5, "tokens_used": 100,
                 "duration_ms": 80, "interaction_id": "i-9", "status": "completed"}]
        ij._schedule_partial_cost(job, req, done, "Failed")
        kw = rec.call_args.kwargs
        assert kw["kind"] == "invoke_async"
        assert kw["result"]["final_state"] == "Failed"
        assert kw["result"]["pipeline_steps"][0]["cost_usd"] == 0.5

    def test_sem_steps_e_noop(self, monkeypatch):
        import app.core.invoke_jobs as ij
        agendou = MagicMock()
        monkeypatch.setattr("app.core.analytics_tasks.schedule_analytics", agendou)
        ij._schedule_partial_cost({"id": "j"}, {}, [], "Failed")
        agendou.assert_not_called()

    def test_os_3_aborts_preservam_custo(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        # timeout, ValueError(Rejected), Exception(Failed) todos chamam o helper
        assert src.count("_schedule_partial_cost(job, req, _done_steps,") == 3


class TestM2ContextVarSemHeranca:
    @pytest.mark.asyncio
    async def test_segunda_op_sem_customer_nao_herda_da_primeira(self, monkeypatch):
        """Loop na MESMA task: caso 1 com customer, caso 2 sem → caso 2 vê None."""
        import app.agents.engine as eng
        from app.core.interaction_access import interaction_customer_hash_for_creation

        async def fake_agents(aid):
            return {"id": aid, "name": aid, "status": "active", "kind": "subagent",
                    "model": "gpt-4o", "skill_id": "sk1", "system_prompt": "p"}
        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agents)
        monkeypatch.setattr("app.core.database.mesh_repo.find_all",
                            AsyncMock(return_value=[]))
        visto = []

        async def fake_exec(**kw):
            visto.append(interaction_customer_hash_for_creation())
            return {"output": "ok", "final_state": "Done", "interaction_id": None,
                    "duration_ms": 1, "evidence_score": 0, "transitions": [], "trace": {}}
        monkeypatch.setattr("app.agents.engine.execute_interaction", fake_exec)

        # caso 1 COM titular, caso 2 SEM — sequencial na mesma task
        await eng.execute_pipeline(entry_agent_id="T", user_input="1", customer_ref="cliente-A")
        await eng.execute_pipeline(entry_agent_id="T", user_input="2")  # sem customer_ref
        from app.core.retention import hash_customer_ref
        assert visto[0] == hash_customer_ref("cliente-A")
        assert visto[1] is None  # NÃO herdou o cliente-A

    def test_set_incondicional_nas_2_funcoes(self):
        src = Path("app/agents/engine.py").read_text(encoding="utf-8")
        # sem o guard `if owner_user_id:` antes do set (set sempre)
        assert "set_interaction_owner_for_creation(owner_user_id)\n" in src
        assert src.count("set_interaction_owner_for_creation(owner_user_id)") == 2


class TestM3CaronasComTimeout:
    def test_caronas_tem_wait_for(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert "_CARONA_TIMEOUT_S" in src
        assert "asyncio.wait_for(sweep_pending(), timeout=_CARONA_TIMEOUT_S)" in src
        assert "asyncio.wait_for(maybe_purge(), timeout=_CARONA_TIMEOUT_S)" in src
        # reap_once (o despacho) NÃO tem timeout — é o trabalho principal
        assert "asyncio.wait_for(reap_once" not in src


def _closed():
    async def _c():
        pass
    return _c()
