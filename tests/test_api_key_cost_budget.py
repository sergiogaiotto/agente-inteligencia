"""Quota de custo por API Key (F6).

Cada invoke via X-API-Key é debitado no ledger de custo da key; ao estourar o
teto da janela corrente (dia|mês|acumulado), novos invokes recebem 402. Tudo
gated por `api_key_cost_budget_enabled` (default OFF = comportamento atual).

Cobertura:
- puras: janela (`_window_start`), coerção (`normalize_window`), soma de custo
  dos steps (`cost_and_tokens_from_result`);
- `enforce_budget`: OFF no-op, sem teto no-op, sob teto passa, no/acima do teto 402;
- `record_cost`: OFF não grava, ON grava linha com os campos certos;
- helpers do invoke (`_guard_api_key_cost_budget`/`_debit_api_key_cost`) só agem
  quando o principal veio por key;
- validação dos campos de orçamento na rota (`_validate_budget`).
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.core import api_key_budget as bud


# ─────────────────────────── funções puras ───────────────────────────
class TestWindowStart:
    _NOW = datetime(2026, 7, 15, 13, 45, 30, 123456)

    def test_day_zera_hora(self):
        assert bud._window_start("day", self._NOW) == datetime(2026, 7, 15, 0, 0, 0, 0)

    def test_month_vai_pro_dia_1(self):
        assert bud._window_start("month", self._NOW) == datetime(2026, 7, 1, 0, 0, 0, 0)

    def test_total_sem_filtro(self):
        assert bud._window_start("total", self._NOW) is None

    def test_desconhecido_cai_em_month(self):
        # normalize_window coage lixo → 'month'
        assert bud._window_start("semana", self._NOW) == datetime(2026, 7, 1, 0, 0, 0, 0)


class TestNormalizeWindow:
    @pytest.mark.parametrize("raw,expected", [
        ("day", "day"), ("MONTH", "month"), (" total ", "total"),
        ("", "month"), (None, "month"), ("xpto", "month"),
    ])
    def test_normalize(self, raw, expected):
        assert bud.normalize_window(raw) == expected


class TestCostFromResult:
    def test_soma_steps(self):
        result = {"pipeline_steps": [
            {"cost_usd": 0.01, "tokens_used": 100},
            {"cost_usd": 0.02, "tokens_used": 50},
        ]}
        cost, tokens = bud.cost_and_tokens_from_result(result)
        assert round(cost, 6) == 0.03
        assert tokens == 150

    def test_ignora_malformados(self):
        result = {"pipeline_steps": [
            {"cost_usd": 0.01, "tokens_used": 100},
            {"cost_usd": None, "tokens_used": "x"},   # None→0 ; "x"→ignora
            {"bad": 1},                                 # sem chaves → 0/0
            "não é dict",                               # ignorado
        ]}
        cost, tokens = bud.cost_and_tokens_from_result(result)
        assert round(cost, 6) == 0.01
        assert tokens == 100

    def test_vazio_e_ausente(self):
        assert bud.cost_and_tokens_from_result({}) == (0.0, 0)
        assert bud.cost_and_tokens_from_result({"pipeline_steps": []}) == (0.0, 0)
        assert bud.cost_and_tokens_from_result(None) == (0.0, 0)

    def test_fallback_agente_single_do_trace(self):
        """Agente single (execute_interaction, SEM pipeline_steps): custo deriva
        do trace (tokens + provider/model REAIS). Sem isto, invoke de subagente
        via key debitava 0 (furava a quota F6 no /agents/{id}/invoke). azure/gpt-4o
        tem preço em llm_pricing (0.0125 / 1k in + 1k out)."""
        result = {"trace": {
            "agent_provider": "azure", "agent_model": "gpt-4o",
            "tokens": {"input": 1000, "output": 1000, "total": 2000},
        }}
        cost, tokens = bud.cost_and_tokens_from_result(result)
        assert cost > 0.0                      # gpt-4o é pago → não é mais 0
        assert round(cost, 6) == 0.0125
        assert tokens == 2000

    def test_fallback_trace_sem_tokens_e_gptoss_zero(self):
        # trace vazio → 0/0
        assert bud.cost_and_tokens_from_result({"trace": {}}) == (0.0, 0)
        # gpt-oss é $0 no pricing, mas os tokens ainda contam
        r = {"trace": {"agent_provider": "openai", "agent_model": "gpt-oss-120b",
                       "tokens": {"input": 10, "output": 5, "total": 15}}}
        cost, tokens = bud.cost_and_tokens_from_result(r)
        assert cost == 0.0 and tokens == 15

    def test_pipeline_steps_tem_precedencia_sobre_trace(self):
        # quando há steps, usa os steps (não o trace) — preserva o comportamento
        # do invoke de pipeline.
        result = {"pipeline_steps": [{"cost_usd": 0.02, "tokens_used": 50}],
                  "trace": {"agent_provider": "azure", "agent_model": "gpt-4o",
                            "tokens": {"input": 9999, "output": 9999}}}
        cost, tokens = bud.cost_and_tokens_from_result(result)
        assert round(cost, 6) == 0.02 and tokens == 50


# ─────────────────────────── enforce_budget ───────────────────────────
def _set_enabled(monkeypatch, enabled: bool):
    class _S:
        api_key_cost_budget_enabled = enabled
    monkeypatch.setattr("app.core.config.get_settings", lambda: _S())


def _set_key(monkeypatch, budget, window="month", spent=0.0):
    async def _fake_budget(_id):
        return budget, window
    async def _fake_spend(_id, _w):
        return spent
    monkeypatch.setattr("app.core.api_key_budget.get_key_budget", _fake_budget)
    monkeypatch.setattr("app.core.api_key_budget.current_spend_usd", _fake_spend)


class TestEnforceBudget:
    @pytest.mark.asyncio
    async def test_sem_key_id_noop(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        await bud.enforce_budget(None)  # não levanta

    @pytest.mark.asyncio
    async def test_toggle_off_nunca_bloqueia(self, monkeypatch):
        _set_enabled(monkeypatch, False)
        _set_key(monkeypatch, budget=1.0, spent=99.0)  # muito acima
        await bud.enforce_budget("k1")  # OFF → passa

    @pytest.mark.asyncio
    async def test_sem_teto_nao_bloqueia(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        _set_key(monkeypatch, budget=None, spent=99.0)
        await bud.enforce_budget("k1")  # budget None → passa

    @pytest.mark.asyncio
    async def test_teto_zero_ignorado(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        _set_key(monkeypatch, budget=0.0, spent=99.0)
        await bud.enforce_budget("k1")  # budget<=0 → passa

    @pytest.mark.asyncio
    async def test_sob_teto_passa(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        _set_key(monkeypatch, budget=5.0, spent=4.99)
        await bud.enforce_budget("k1")  # 4.99 < 5 → passa

    @pytest.mark.asyncio
    async def test_no_teto_bloqueia_402(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        _set_key(monkeypatch, budget=5.0, window="month", spent=5.0)
        with pytest.raises(HTTPException) as ei:
            await bud.enforce_budget("k1")
        assert ei.value.status_code == 402
        d = ei.value.detail
        assert d["error"] == "cost_budget_exceeded"
        assert d["reason"] == "api_key_cost_budget"
        assert d["budget_usd"] == 5.0
        assert d["spent_usd"] == 5.0
        assert d["window"] == "month"

    @pytest.mark.asyncio
    async def test_acima_do_teto_bloqueia(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        _set_key(monkeypatch, budget=1.0, spent=1.5)
        with pytest.raises(HTTPException) as ei:
            await bud.enforce_budget("k1")
        assert ei.value.status_code == 402


# ─────────────────────────── record_cost ───────────────────────────
class _FakeLedgerRepo:
    def __init__(self):
        self.rows = []
    async def create(self, data):
        self.rows.append(data)
        return data


class TestRecordCost:
    @pytest.mark.asyncio
    async def test_off_nao_grava(self, monkeypatch):
        _set_enabled(monkeypatch, False)
        fake = _FakeLedgerRepo()
        monkeypatch.setattr("app.core.database.api_key_cost_ledger_repo", fake)
        await bud.record_cost("k1", 0.05, 200, pipeline_id="p1", interaction_id="i1")
        assert fake.rows == []

    @pytest.mark.asyncio
    async def test_sem_key_nao_grava(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        fake = _FakeLedgerRepo()
        monkeypatch.setattr("app.core.database.api_key_cost_ledger_repo", fake)
        await bud.record_cost(None, 0.05, 200)
        assert fake.rows == []

    @pytest.mark.asyncio
    async def test_on_grava_linha(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        fake = _FakeLedgerRepo()
        monkeypatch.setattr("app.core.database.api_key_cost_ledger_repo", fake)
        await bud.record_cost("k1", 0.05, 200, pipeline_id="p1", interaction_id="i1")
        assert len(fake.rows) == 1
        row = fake.rows[0]
        assert row["api_key_id"] == "k1"
        assert row["cost_usd"] == 0.05
        assert row["tokens_used"] == 200
        assert row["pipeline_id"] == "p1"
        assert row["interaction_id"] == "i1"
        assert row["id"] and isinstance(row["created_at"], datetime)
        assert row["created_at"].tzinfo is None  # UTC naive (coluna TIMESTAMP)

    @pytest.mark.asyncio
    async def test_falha_de_gravacao_e_engolida(self, monkeypatch):
        _set_enabled(monkeypatch, True)
        class _Boom:
            async def create(self, data):
                raise RuntimeError("db down")
        monkeypatch.setattr("app.core.database.api_key_cost_ledger_repo", _Boom())
        # Best-effort: não pode propagar (o invoke já executou).
        await bud.record_cost("k1", 0.05, 200)


# ───────────────────── helpers do invoke (pipelines) ─────────────────────
class _State:
    pass


class _Req:
    def __init__(self, api_key_id=None):
        self.state = _State()
        if api_key_id:
            self.state.api_key_id = api_key_id


class TestInvokeHelpers:
    @pytest.mark.asyncio
    async def test_guard_noop_sem_key(self, monkeypatch):
        from app.routes import pipelines
        called = {"n": 0}
        async def _fake_enforce(_id):
            called["n"] += 1
        monkeypatch.setattr("app.core.api_key_budget.enforce_budget", _fake_enforce)
        await pipelines._guard_api_key_cost_budget(_Req(api_key_id=None))
        assert called["n"] == 0  # sessão de UI não paga quota

    @pytest.mark.asyncio
    async def test_guard_chama_enforce_com_key(self, monkeypatch):
        from app.routes import pipelines
        seen = {}
        async def _fake_enforce(_id):
            seen["id"] = _id
        monkeypatch.setattr("app.core.api_key_budget.enforce_budget", _fake_enforce)
        await pipelines._guard_api_key_cost_budget(_Req(api_key_id="k9"))
        assert seen["id"] == "k9"

    @pytest.mark.asyncio
    async def test_debit_noop_sem_key(self, monkeypatch):
        from app.routes import pipelines
        called = {"n": 0}
        async def _fake_record(*a, **k):
            called["n"] += 1
        monkeypatch.setattr("app.core.api_key_budget.record_cost", _fake_record)
        await pipelines._debit_api_key_cost(_Req(None), "p1", {"pipeline_steps": []})
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_debit_soma_e_grava(self, monkeypatch):
        from app.routes import pipelines
        seen = {}
        async def _fake_record(api_key_id, cost, tokens, pipeline_id=None, interaction_id=None):
            seen.update(dict(api_key_id=api_key_id, cost=cost, tokens=tokens,
                             pipeline_id=pipeline_id, interaction_id=interaction_id))
        monkeypatch.setattr("app.core.api_key_budget.record_cost", _fake_record)
        result = {
            "interaction_id": "int-42",
            "pipeline_steps": [
                {"cost_usd": 0.02, "tokens_used": 120},
                {"cost_usd": 0.01, "tokens_used": 80},
            ],
        }
        await pipelines._debit_api_key_cost(_Req("k9"), "p1", result)
        assert seen["api_key_id"] == "k9"
        assert round(seen["cost"], 6) == 0.03
        assert seen["tokens"] == 200
        assert seen["pipeline_id"] == "p1"
        assert seen["interaction_id"] == "int-42"


# ───────────────────── validação de orçamento na rota ─────────────────────
class TestValidateBudget:
    def test_valido(self):
        from app.routes.api_keys import _validate_budget
        assert _validate_budget(5.0, "month") == "month"
        assert _validate_budget(None, "day") == "day"       # None = sem teto
        assert _validate_budget(0.01, "total") == "total"

    def test_negativo_400(self):
        from app.routes.api_keys import _validate_budget
        with pytest.raises(HTTPException) as ei:
            _validate_budget(-1, "month")
        assert ei.value.status_code == 400

    def test_zero_400(self):
        # 0 não é "sem teto" (isso é None) nem um teto útil → rejeitado.
        from app.routes.api_keys import _validate_budget
        with pytest.raises(HTTPException) as ei:
            _validate_budget(0, "month")
        assert ei.value.status_code == 400

    def test_nao_finito_400(self):
        # NaN/Infinity furam a governança (nunca bloqueiam) → rejeitados na entrada.
        from app.routes.api_keys import _validate_budget
        for bad in (float("inf"), float("-inf"), float("nan")):
            with pytest.raises(HTTPException) as ei:
                _validate_budget(bad, "month")
            assert ei.value.status_code == 400

    def test_janela_invalida_400(self):
        from app.routes.api_keys import _validate_budget
        with pytest.raises(HTTPException) as ei:
            _validate_budget(5.0, "semana")
        assert ei.value.status_code == 400

    def test_janela_default_quando_vazia(self):
        from app.routes.api_keys import _validate_budget
        assert _validate_budget(5.0, None) == "month"
        assert _validate_budget(5.0, "") == "month"
