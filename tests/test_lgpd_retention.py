"""Arco LGPD-1 (35.8.0) — retenção de conversas por idade.

Decisão do dono: DELETE das interactions antigas (cascade turns/tool_calls/
binding) + SCRUB do texto das verifications (preserva a linha analítica do
juiz → /quality e drift sobrevivem) + varre api_call_logs/verifier_jobs.
Default OFF (0 = desligado). Carona no reaper, throttle ~1x/hora.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import retention


@pytest.fixture(autouse=True)
def _limpo():
    retention._reset_for_tests()
    yield
    retention._reset_for_tests()


class FakeCon:
    def __init__(self, ids):
        self.calls: list = []
        self._ids = ids

    async def fetch(self, sql, *a):
        self.calls.append(("fetch", sql, a))
        if "uploaded_files" in sql:  # 35.15.0 G: sem arquivos neste fake
            return []
        return [{"id": i} for i in self._ids]

    async def execute(self, sql, *a):
        self.calls.append(("execute", sql, a))
        # rowcount string no formato do asyncpg
        if "DELETE FROM interactions" in sql:
            return f"DELETE {len(self._ids)}"
        if "UPDATE verifications" in sql:
            return f"UPDATE {len(self._ids)}"
        if "DELETE FROM invoke_jobs" in sql:  # 35.14.6: purga por idade
            return f"DELETE {len(self._ids)}"
        return "DELETE 0"

    def sql(self, frag):
        return [c for c in self.calls if frag in c[1]]

    def transaction(self):  # 35.14.2: _purge_ids agora é atômico
        con = self
        class _Tx:
            async def __aenter__(self):
                con.calls.append(("tx", "BEGIN", ()))
                return con
            async def __aexit__(self, *a):
                con.calls.append(("tx", "COMMIT" if a[0] is None else "ROLLBACK", ()))
                return False
        return _Tx()


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


def _wire(monkeypatch, *, days, ids=("i1", "i2")):
    con = FakeCon(list(ids))
    monkeypatch.setattr("app.core.database._get_pool", lambda: FakePool(con))
    monkeypatch.setattr("app.core.config.get_settings",
                        lambda: SimpleNamespace(interactions_retention_days=days))
    return con


class TestPurga:
    @pytest.mark.asyncio
    async def test_desligado_e_noop(self, monkeypatch):
        con = _wire(monkeypatch, days=0)
        out = await retention.purge_interactions_once()
        assert out == {"deleted": 0, "scrubbed_verifications": 0, "purged_jobs": 0}
        assert con.calls == []  # nem toca o banco

    @pytest.mark.asyncio
    async def test_ordem_segura_scrub_antes_do_delete(self, monkeypatch):
        con = _wire(monkeypatch, days=90)
        out = await retention.purge_interactions_once()
        assert out["deleted"] == 2 and out["scrubbed_verifications"] == 2
        # verifications são SCRUBADAS (UPDATE), nunca deletadas
        assert con.sql("UPDATE verifications")
        assert not con.sql("DELETE FROM verifications")
        # ordem: o scrub das verifications vem ANTES do delete das interactions
        idx_scrub = next(i for i, c in enumerate(con.calls) if "UPDATE verifications" in c[1])
        idx_del = next(i for i, c in enumerate(con.calls) if "DELETE FROM interactions" in c[1])
        assert idx_scrub < idx_del
        # varre as órfãs que não cascateiam
        assert con.sql("DELETE FROM api_call_logs")
        assert con.sql("DELETE FROM verifier_jobs")
        # FinOps FICA: nunca toca ledgers/custos
        assert not con.sql("invocation_costs")
        assert not con.sql("api_key_cost_ledger")
        # janela em DIAS aplicada no SELECT
        sel = con.sql("interval '1 day'")[0]
        assert sel[2][0] == 90.0

    @pytest.mark.asyncio
    async def test_purga_invoke_jobs_por_idade(self, monkeypatch):
        # 35.14.6 (achado de auditoria): a retenção por idade também apaga os
        # invoke_jobs velhos — o request_payload guarda a conversa CRUA e sem
        # isto sobrevivia à janela prometida (até o reaper de jobs, dias além).
        con = _wire(monkeypatch, days=90)
        out = await retention.purge_interactions_once()
        js = con.sql("DELETE FROM invoke_jobs")
        assert js, "invoke_jobs deve ser purgado por idade"
        assert js[0][2][0] == 90.0  # MESMA janela em dias do SELECT de interactions
        assert out["purged_jobs"] == 2
        # FinOps continua intocado (invocation_costs nunca joina/apaga)
        assert not con.sql("invocation_costs")

    @pytest.mark.asyncio
    async def test_lote_vazio_para_cedo(self, monkeypatch):
        con = _wire(monkeypatch, days=30, ids=())
        out = await retention.purge_interactions_once()
        assert out == {"deleted": 0, "scrubbed_verifications": 0,
                       "purged_jobs": 0, "purged_files": 0}
        assert con.sql("SELECT id FROM interactions")  # consultou
        assert not con.sql("DELETE FROM interactions")  # mas nada a apagar


class TestThrottle:
    @pytest.mark.asyncio
    async def test_maybe_purge_1x_por_intervalo(self, monkeypatch):
        _wire(monkeypatch, days=90)
        chamadas = {"n": 0}

        async def _fake_purge():
            chamadas["n"] += 1
            return {"deleted": 1, "scrubbed_verifications": 0}
        monkeypatch.setattr(retention, "purge_interactions_once", _fake_purge)
        # relógio monotônico controlado
        t = {"v": 1000.0}
        monkeypatch.setattr(retention, "_now_monotonic", lambda: t["v"])

        assert await retention.maybe_purge() is not None   # 1ª roda
        assert await retention.maybe_purge() is None        # throttle: <1h
        t["v"] += retention._PURGE_MIN_INTERVAL_S + 1
        assert await retention.maybe_purge() is not None   # passou 1h → roda
        assert chamadas["n"] == 2

    @pytest.mark.asyncio
    async def test_maybe_purge_desligado_nao_marca_throttle(self, monkeypatch):
        _wire(monkeypatch, days=0)
        assert await retention.maybe_purge() is None
        assert retention._last_purge_at is None  # não consome a janela


class TestFiacao:
    def test_setting_nos_7_toques(self):
        from app.core.config import _UI_TO_ENV_MAP, PARAMETER_UI_KEYS
        from app.core.config import Settings
        assert "interactions_retention_days" in Settings.model_fields
        assert _UI_TO_ENV_MAP["interactions_retention_days"] == "INTERACTIONS_RETENTION_DAYS"
        assert "interactions_retention_days" in PARAMETER_UI_KEYS

    def test_carona_no_reaper(self):
        src = Path("app/core/invoke_jobs.py").read_text(encoding="utf-8")
        assert "from app.core.retention import maybe_purge" in src
        assert "asyncio.wait_for(maybe_purge()" in src  # 35.14.4: com teto de latência
        # try/except próprio (não derruba o reaper)
        assert "event=retention_purge_failed" in src

    def test_default_off(self):
        from app.core.config import Settings
        assert Settings.model_fields["interactions_retention_days"].default == 0
