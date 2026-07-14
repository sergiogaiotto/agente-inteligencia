"""Minors da auditoria adversarial de estado-integrado (35.14.6).

- Minor 3 (LGPD-2): reuso de sessão carimba customer_hash retroativamente
  (first-writer-wins) — senão uma interaction nascida sem pivô nunca é alcançada
  por forget_customer.
- Minor 4 (RED off-path): custo PARCIAL de um aborto não conta um 2º erro no
  Prometheus (o _record_async_failure do MESMO branch já conta) — via
  emit_metrics=False em _record_invoke_analytics.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


# ─────────────────────────── Minor 3: stamp no reuso ───────────────────────────

class _Con:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *a):
        self.calls.append((sql, a))
        return "UPDATE 1"


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
async def test_stamp_customer_hash_first_writer_wins(monkeypatch):
    from app.core import interaction_access as IA
    con = _Con()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))

    await IA.stamp_interaction_customer_hash("i1", "h1")
    assert con.calls, "deve emitir o UPDATE de carimbo"
    sql, args = con.calls[0]
    assert "UPDATE interactions" in sql and "customer_hash" in sql
    assert "customer_hash IS NULL" in sql  # first-writer-wins: não sobrescreve
    assert args == ("h1", "i1")


@pytest.mark.asyncio
async def test_stamp_customer_hash_noop_sem_id_ou_hash(monkeypatch):
    from app.core import interaction_access as IA
    con = _Con()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _Pool(con))
    await IA.stamp_interaction_customer_hash(None, "h1")
    await IA.stamp_interaction_customer_hash("i1", None)
    await IA.stamp_interaction_customer_hash("i1", "")
    assert con.calls == []  # nada a fazer → nem toca o banco


def test_run_intake_carimba_customer_hash_no_reuso():
    """O ramo de REUSO (existing) do run_intake carimba o pivô — guarda
    estrutural contra regressão (o ramo antes só fazia update(state))."""
    src = Path("app/agents/state_machine.py").read_text(encoding="utf-8")
    assert "stamp_interaction_customer_hash" in src
    # a leitura do ContextVar do titular acontece no reuso, não só na criação
    assert src.count("interaction_customer_hash_for_creation") >= 2


# ───────────────────── Minor 4: RED não duplica no custo parcial ─────────────────

@pytest.mark.asyncio
async def test_emit_metrics_false_nao_conta_red(monkeypatch):
    """emit_metrics=False pula SÓ o record_invocation (RED); auditoria/ledger
    seguem. Fecha a dupla-contagem: no aborto com steps parciais, o custo parcial
    NÃO conta um 2º erro (o _record_async_failure do branch já conta)."""
    from app.routes import pipelines as P

    # Neutraliza os off-path pesados (não são o alvo — só o RED é).
    monkeypatch.setattr(P.audit_repo, "create", AsyncMock())
    monkeypatch.setattr(P, "_attribute_interaction_to_key", AsyncMock())
    monkeypatch.setattr(P, "_debit_api_key_cost", AsyncMock())
    monkeypatch.setattr("app.core.cost_ledger.record_invocation_cost", AsyncMock())
    monkeypatch.setattr("app.core.api_key_budget.cost_and_tokens_from_result",
                        lambda r: (0.0, 0))
    spy = MagicMock()
    monkeypatch.setattr("app.core.metrics.record_invocation", spy)

    result = {"status": "failed", "final_state": "JobTimeout", "duration_ms": 10}
    kw = dict(pid="p", root="r", member_count=1, result=result, api_key_id=None,
              api_key_name=None, actor_user_id="u", arg_keys=[], kind="invoke_async")

    # custo parcial de aborto → emit_metrics=False → NÃO conta RED
    await P._record_invoke_analytics(**kw, emit_metrics=False)
    assert spy.call_count == 0
    # ledger/atribuição seguem rodando (não são suprimidos)
    assert P._debit_api_key_cost.await_count == 1

    # caminho normal (default True) → conta RED uma vez
    await P._record_invoke_analytics(**kw)
    assert spy.call_count == 1


def test_schedule_partial_cost_suprime_red():
    """_schedule_partial_cost passa emit_metrics=False — guarda estrutural: sem
    o flag, cada aborto com steps parciais contaria erro 2x (custo parcial +
    _record_async_failure)."""
    src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
    assert "emit_metrics=False" in src
    # o flag vive DENTRO do _schedule_partial_cost (não solto no arquivo)
    fn = src[src.index("def _schedule_partial_cost"):]
    fn = fn[:fn.index("\nasync def ") if "\nasync def " in fn else len(fn)]
    assert "emit_metrics=False" in fn
