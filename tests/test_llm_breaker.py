"""Circuit-breaker do egress LLM (33.1.0) — app/core/llm_breaker.py.

Testa a máquina de estados no backend in-process (determinístico, sem Redis) com
relógio controlado, o passthrough quando desligado, o isolamento por-provider, o
contrato de config (flags não-seladas) e a integração-chave: um circuito ABERTO
faz ``generate_with_hosted_fallback`` PULAR o primário (sem pagar o timeout) e cair
direto no fallback.
"""

from __future__ import annotations

import os

import pytest

from app.core import config as _config
from app.core import llm_breaker
from app.core.llm_breaker import CircuitBreaker, _MemoryBreaker


# ═════════════════════════════════════════════════════════════════
# Fixtures
# ═════════════════════════════════════════════════════════════════


@pytest.fixture
def cb_settings():
    """Fixa os thresholds do breaker via env (threshold=3, cooldown=30, probes=1)
    e restaura os.environ + cache de get_settings ao final."""
    saved = dict(os.environ)
    _config.get_settings.cache_clear()
    os.environ["CIRCUIT_BREAKER_ENABLED"] = "true"
    os.environ["CB_FAILURE_THRESHOLD"] = "3"
    os.environ["CB_COOLDOWN_SECONDS"] = "30"
    os.environ["CB_HALF_OPEN_MAX_PROBES"] = "1"
    _config.get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
        _config.get_settings.cache_clear()


@pytest.fixture
def clock(monkeypatch):
    """Relógio monotônico controlável — avança sem sleep real."""
    t = {"now": 1000.0}
    monkeypatch.setattr(llm_breaker, "_now", lambda: t["now"])
    return t


# ═════════════════════════════════════════════════════════════════
# Máquina de estados (backend memory, determinístico)
# ═════════════════════════════════════════════════════════════════


class TestStateMachine:
    @pytest.mark.asyncio
    async def test_abaixo_do_threshold_fica_fechado(self, cb_settings, clock):
        b = _MemoryBreaker()
        await b.record_failure("azure")
        await b.record_failure("azure")  # 2 < 3
        assert await b.is_open("azure") is False
        assert await b.allow("azure") is True

    @pytest.mark.asyncio
    async def test_threshold_abre_e_curto_circuita(self, cb_settings, clock):
        b = _MemoryBreaker()
        for _ in range(3):
            await b.record_failure("azure")
        assert await b.is_open("azure") is True
        assert await b.allow("azure") is False  # chamadas pulam sem pagar timeout

    @pytest.mark.asyncio
    async def test_sucesso_zera_o_contador(self, cb_settings, clock):
        b = _MemoryBreaker()
        await b.record_failure("azure")
        await b.record_failure("azure")
        await b.record_success("azure")  # reset
        await b.record_failure("azure")
        await b.record_failure("azure")  # 2 de novo, não 4
        assert await b.is_open("azure") is False

    @pytest.mark.asyncio
    async def test_half_open_apos_cooldown_concede_uma_sonda(self, cb_settings, clock):
        b = _MemoryBreaker()
        for _ in range(3):
            await b.record_failure("azure")
        assert await b.is_open("azure") is True
        clock["now"] += 31  # cooldown de 30s passou
        # is_open (peek) é False em half-open — não estamos mais OPEN:
        assert await b.is_open("azure") is False
        assert await b.allow("azure") is True   # 1ª sonda concedida
        assert await b.allow("azure") is False  # 2ª bloqueada (max_probes=1)

    @pytest.mark.asyncio
    async def test_sonda_que_falha_reabre(self, cb_settings, clock):
        b = _MemoryBreaker()
        for _ in range(3):
            await b.record_failure("azure")
        clock["now"] += 31
        assert await b.allow("azure") is True   # sonda
        await b.record_failure("azure")         # sonda falhou → reabre imediato
        assert await b.is_open("azure") is True
        assert await b.allow("azure") is False

    @pytest.mark.asyncio
    async def test_sonda_que_passa_fecha(self, cb_settings, clock):
        b = _MemoryBreaker()
        for _ in range(3):
            await b.record_failure("azure")
        clock["now"] += 31
        assert await b.allow("azure") is True   # sonda
        await b.record_success("azure")         # sonda passou → fecha
        assert await b.is_open("azure") is False
        assert await b.allow("azure") is True

    @pytest.mark.asyncio
    async def test_isolamento_por_provider(self, cb_settings, clock):
        b = _MemoryBreaker()
        for _ in range(3):
            await b.record_failure("azure")
        assert await b.is_open("azure") is True
        assert await b.is_open("gpt-oss-20b") is False  # outro circuito intacto


# ═════════════════════════════════════════════════════════════════
# Facade CircuitBreaker — flag master + fail-open
# ═════════════════════════════════════════════════════════════════


class TestFacade:
    @pytest.mark.asyncio
    async def test_desligado_e_passthrough_total(self, cb_settings, clock, monkeypatch):
        monkeypatch.setenv("CIRCUIT_BREAKER_ENABLED", "false")
        _config.get_settings.cache_clear()
        cb = CircuitBreaker()
        cb._impl = _MemoryBreaker()
        for _ in range(5):
            await cb.record_failure("azure")  # no-op quando desligado
        assert await cb.is_open("azure") is False
        assert await cb.allow("azure") is True

    @pytest.mark.asyncio
    async def test_ligado_abre_via_memory(self, cb_settings, clock):
        cb = CircuitBreaker()
        cb._impl = _MemoryBreaker()  # força memory (não depende de Redis no CI)
        for _ in range(3):
            await cb.record_failure("gpt-oss-120b")
        assert await cb.is_open("gpt-oss-120b") is True
        assert await cb.allow("gpt-oss-120b") is False


# ═════════════════════════════════════════════════════════════════
# Contrato de config — flags de comportamento, não-seladas
# ═════════════════════════════════════════════════════════════════


def test_cb_flags_sao_comportamento_nao_seladas():
    keys = (
        "circuit_breaker_enabled",
        "cb_failure_threshold",
        "cb_cooldown_seconds",
        "cb_half_open_max_probes",
    )
    for k in keys:
        assert k in _config._UI_TO_ENV_MAP, k
        assert k in _config._NON_MODEL_UI_KEYS, k
        # comportamento → o env NÃO é selado (vale como fallback)
        assert _config._UI_TO_ENV_MAP[k] not in _config._SEALED_ENV_VARS, k


# ═════════════════════════════════════════════════════════════════
# Integração — o circuito aberto PULA o primário (timeout evitado)
# ═════════════════════════════════════════════════════════════════


class TestHostedFallbackIntegration:
    @pytest.mark.asyncio
    async def test_primario_aberto_pula_direto_pro_fallback(self, cb_settings, clock, monkeypatch):
        from app.core import llm_providers
        from app.core.llm_breaker import breaker

        # Força memory e ABRE o circuito do primário.
        breaker._impl = _MemoryBreaker()
        canon = llm_providers.canonical_provider("gpt-oss-120b")
        try:
            for _ in range(3):
                await breaker.record_failure(canon)

            calls: list[str] = []

            class FakeLLM:
                model = "m"

                async def generate(self, messages, **gk):
                    calls.append("gen")
                    return {"content": "ok"}

            def fake_get_provider(name, model=None, **kw):
                calls.append(f"get:{name}")
                return FakeLLM()

            async def fake_load_routing():
                return {"multimodal_fallback": "azure/gpt-4o"}

            monkeypatch.setattr(llm_providers, "get_provider", fake_get_provider)
            monkeypatch.setattr("app.llm_routing.load_routing", fake_load_routing)

            resp, used_p, used_m = await llm_providers.generate_with_hosted_fallback(
                [{"role": "user", "content": "x"}],
                "gpt-oss-120b",
                "gpt-oss-120b",
                purpose="test.breaker",
            )

            assert used_p == "azure"               # caiu no fallback hospedado
            assert resp["content"] == "ok"
            # NUNCA instanciou o primário → o timeout do provider morto foi evitado.
            assert "get:gpt-oss-120b" not in calls
            assert "get:azure" in calls
        finally:
            breaker._impl = None  # restaura seleção lazy p/ não vazar entre testes
