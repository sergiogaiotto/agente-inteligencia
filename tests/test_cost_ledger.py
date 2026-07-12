"""SSOT de custo por invocação — invocation_costs + agregação, escrito OFF-PATH.

Cobre TODOS os caminhos de invoke (inclusive cookie/UI, que nenhum ledger via
antes). A escrita vive dentro de _record_invoke_analytics (que o invoke agenda
detached), então NÃO paga o caminho de resposta. O endpoint /dashboard/costs é a
visão org-wide de "quanto gastamos" (role-gated).
"""
from pathlib import Path

import pytest


class _FakeConn:
    def __init__(self):
        self.calls = []
        self._rows = []
        self._row = None

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self._rows

    async def fetchrow(self, sql, *args):
        self.calls.append(("fetchrow", sql, args))
        return self._row


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_record_insere_linha(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(conn))
    from app.core.cost_ledger import record_invocation_cost
    await record_invocation_cost(
        interaction_id="i1", pipeline_id="p1", user_id="u1", source="invoke",
        cost_usd=0.03, tokens_used=200, latency_ms=1500, final_state="LogAndClose",
    )
    op, sql, args = conn.calls[0]
    assert op == "execute" and "INSERT INTO invocation_costs" in sql
    assert "i1" in args and "p1" in args and 0.03 in args and 200 in args and "invoke" in args


@pytest.mark.asyncio
async def test_aggregate_group_by_invalido():
    from app.core.cost_ledger import aggregate_invocation_costs
    with pytest.raises(ValueError):
        await aggregate_invocation_costs(group_by="bogus")


@pytest.mark.asyncio
async def test_aggregate_monta_query_e_totais(monkeypatch):
    conn = _FakeConn()
    conn._rows = [{"group_key": "p1", "invocations": 3, "total_cost_usd": 0.09,
                   "total_tokens": 600, "avg_latency_ms": 1400.0}]
    conn._row = {"invocations": 3, "total_cost_usd": 0.09, "total_tokens": 600,
                 "avg_latency_ms": 1400.0}
    monkeypatch.setattr("app.core.database._get_pool", lambda: _FakePool(conn))
    from app.core.cost_ledger import aggregate_invocation_costs
    rows, totals = await aggregate_invocation_costs(group_by="pipeline", since="2026-07-01")
    assert rows[0]["group_key"] == "p1" and totals["total_cost_usd"] == 0.09
    fetch_sql = next(c[1] for c in conn.calls if c[0] == "fetch")
    assert "GROUP BY pipeline_id" in fetch_sql and "created_at >=" in fetch_sql


def test_tabela_no_schema():
    src = Path("app/core/database.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS invocation_costs" in src
    assert "idx_invocation_costs_created_at" in src


def test_invoke_escreve_ledger_offpath():
    src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
    # a escrita do ledger vive DENTRO de _record_invoke_analytics (agendado detached)
    assert "record_invocation_cost(" in src
    assert 'source="invoke_stream" if stream else "invoke"' in src
    assert "_schedule_analytics(_record_invoke_analytics(" in src


def test_endpoint_de_custo_registrado_e_gated():
    src = Path("app/routes/dashboard.py").read_text(encoding="utf-8")
    assert '@router.get("/dashboard/costs")' in src
    assert "aggregate_invocation_costs" in src
    assert 'require_role("root", "admin")' in src
