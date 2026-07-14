"""Fixes da auditoria adversarial #3 — estado integrado 35.14.6 → 35.14.7.

A auditoria de estado-integrado #3 (sobre os fixes #611/#612) achou que DOIS deles
tinham lacuna e um introduziu regressão:

- A (major): o stamp de customer_hash no reuso de sessão (#612) só tocou o FSM;
  o gêmeo da cadeia DECLARATIVA (_run_declarative_as_interaction) ficou de fora.
- B (major): a purga por idade de invoke_jobs (#612) deletava queued/running —
  perda de trabalho aceito + job em voo virando no-op silencioso.
- F (minor): o emit_metrics=False (#612) que fechou a dupla-contagem RED apagou
  TAMBÉM a amostra de latência dos abortos (p95/p99 cegos a timeouts).
- C (minor, pré-existente): POST /agents/{id}/invoke não passava owner_user_id →
  interação órfã (o criador tomava 404 no retry após crash).
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock


# ─────────────── A: stamp no reuso cobre AMBAS as vias de criação ───────────────

def test_stamp_no_reuso_cobre_fsm_e_declarativo():
    fsm = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
    eng = Path("app/agents/engine.py").read_text(encoding="utf-8")
    # FSM (run_intake) e cadeia declarativa (_run_declarative_as_interaction)
    # ambos carimbam o pivô no ramo de reuso.
    assert "stamp_interaction_customer_hash" in fsm
    assert "stamp_interaction_customer_hash" in eng
    # na cadeia declarativa a leitura do pivô aparece 2x: criação + reuso
    assert eng.count("interaction_customer_hash_for_creation") >= 2


# ─────────────── B: purga por idade NÃO apaga queued/running ────────────────────

class _Con:
    def __init__(self, ids):
        self.calls = []
        self._ids = ids

    async def fetch(self, sql, *a):
        self.calls.append((sql, a))
        if "uploaded_files" in sql:  # 35.15.0 G: sem arquivos neste fake
            return []
        return [{"id": i} for i in self._ids]

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        if "DELETE FROM interactions" in sql:
            return f"DELETE {len(self._ids)}"
        if "UPDATE verifications" in sql:
            return f"UPDATE {len(self._ids)}"
        if "DELETE FROM invoke_jobs" in sql:
            return f"DELETE {len(self._ids)}"
        return "DELETE 0"

    def sql(self, frag):
        return [c for c in self.calls if frag in c[0]]

    def transaction(self):
        con = self

        class _Tx:
            async def __aenter__(self):
                return con

            async def __aexit__(self, *a):
                return False
        return _Tx()


class _Pool:
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


@pytest.mark.asyncio
async def test_purga_invoke_jobs_so_terminais(monkeypatch):
    from types import SimpleNamespace
    from app.core import retention
    con = _Con(["i1", "i2"])
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: SimpleNamespace(interactions_retention_days=30))
    await retention.purge_interactions_once()
    dj = con.sql("DELETE FROM invoke_jobs")
    assert dj, "invoke_jobs deve ser purgado por idade"
    # o filtro de status terminal está presente: NÃO apaga queued/running em voo
    assert "status IN ('completed', 'failed', 'lost')" in dj[0][0]


# ─────────────── C: /agents/invoke nasce com dono nos DOIS branches ─────────────

def test_agents_invoke_passa_owner_nos_dois_branches():
    src = Path("app/routes/agents.py").read_text(encoding="utf-8")
    # branch pipeline (execute_pipeline) + branch single (execute_interaction)
    assert src.count('owner_user_id=_caller.get("id")') == 2


# ─────────────── F: aborto pós-execução alimenta a latência RED ─────────────────

def test_record_async_failure_repassa_duracao(monkeypatch):
    from app.core import invoke_jobs
    spy = MagicMock()
    monkeypatch.setattr("app.core.metrics.record_invocation", spy)
    # aborto pós-execução com latência real
    invoke_jobs._record_async_failure("timeout", 12.5)
    assert spy.call_count == 1
    assert spy.call_args.kwargs["duration_s"] == 12.5
    assert spy.call_args.kwargs["error"] is True
    # recheck pré-execução mantém 0.0 (não houve trabalho)
    spy.reset_mock()
    invoke_jobs._record_async_failure("api_key_revoked")
    assert spy.call_args.kwargs["duration_s"] == 0.0


def test_abortos_pos_execucao_passam_duracao_real():
    """Os 3 abortos pós-execução (timeout/rejected/error) medem a latência real
    (time.monotonic - _exec_t0); os rechecks pré-execução, não."""
    src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
    assert src.count("time.monotonic() - _exec_t0") == 3
    assert "_exec_t0 = time.monotonic()" in src
