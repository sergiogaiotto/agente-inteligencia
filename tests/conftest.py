"""Conftest raiz — fixtures compartilhadas.

A Onda 1 cobre apenas testes de lógica pura (Pydantic, parsers, state machines).
Integração com PostgreSQL fica para uma onda futura quando houver investimento
em test DB / fixture container (asyncpg + transação rollbackable).
"""

import os
from pathlib import Path

import pytest


def _load_env_test() -> None:
    """Carrega ``.env.test`` em ``os.environ`` ANTES de qualquer import de
    ``app.core.*`` — torna a suíte HERMÉTICA (33.2.1).

    O ``.env`` local de dev roda ``APP_ENV=production`` (espelha prod). Sem isto,
    o crypto-fail-fast (#559) LANÇAVA em ~7 testes de federação
    (``crypto._get_fernet`` lê ``os.environ['MAESTRO_SECRET_KEY']`` DIRETO — que o
    dotenv do pydantic NÃO popula — e ``is_production()`` True fazia levantar em
    vez do fallback), exigindo o workaround ``APP_ENV=development``.

    Aqui forçamos ``APP_ENV=test`` + SECRET/MAESTRO determinísticos em
    ``os.environ``: a fonte ``env`` do pydantic vence o ``dotenv`` (.env), e o
    crypto lê ``os.environ`` direto. Roda no IMPORT do conftest raiz — antes da
    coleta importar qualquer módulo do app — então o valor certo já está no
    ambiente na 1ª leitura de ``get_settings()``/``_get_fernet()``. Idempotente e
    sem dependência de plugin (não usa pytest-dotenv)."""
    root = Path(__file__).resolve().parent.parent
    env_test = root / ".env.test"
    if not env_test.exists():
        return
    for raw in env_test.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ[key.strip()] = val.strip()


_load_env_test()


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
