"""Conftest raiz — fixtures compartilhadas.

A Onda 1 cobre apenas testes de lógica pura (Pydantic, parsers, state machines).
Integração com PostgreSQL fica para uma onda futura quando houver investimento
em test DB / fixture container (asyncpg + transação rollbackable).
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_circuit_breaker():
    """Neutraliza o circuit-breaker do egress LLM (33.1.0) por padrão na suíte.

    ``app/core/llm_breaker.breaker`` é um singleton de MÓDULO default-ON. Sem
    isolamento, as falhas de alcance SIMULADAS por um teste (cadeias de fallback
    do engine/wizard/hosted) acumulariam no estado GLOBAL e ABRIRIAM o circuito,
    mudando o comportamento de testes SEGUINTES (o primário seria pulado) — a
    ordem dos testes viraria fonte de flakiness.

    Desligado por padrão → os testes existentes se comportam exatamente como
    antes (o breaker é passthrough: is_open→False, allow→True, record_*→no-op,
    sem sequer tocar o backend/Redis). ``tests/test_llm_breaker.py`` reativa
    explicitamente via a fixture ``cb_settings``. O comportamento LIGADO é
    verificado ali e no app real (Docker)."""
    from app.core import config as _config
    from app.core import llm_breaker

    saved = os.environ.get("CIRCUIT_BREAKER_ENABLED")
    os.environ["CIRCUIT_BREAKER_ENABLED"] = "false"
    _config.get_settings.cache_clear()
    llm_breaker.breaker._impl = None
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("CIRCUIT_BREAKER_ENABLED", None)
        else:
            os.environ["CIRCUIT_BREAKER_ENABLED"] = saved
        _config.get_settings.cache_clear()
        llm_breaker.breaker._impl = None
