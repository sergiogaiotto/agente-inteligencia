"""Fixtures pra testes de integração com Postgres real.

Estes testes rodam em job separado do CI (job test-integration em
.github/workflows/test.yml) — pra não atrasar feedback do pytest unit.
Localmente, rodar com:

    docker compose up -d postgres   # garante Postgres pgvector subido
    pytest tests/integration -m integration

Pulam silenciosamente se DATABASE_URL não estiver acessível (TEST_DATABASE_URL
no CI aponta pro service container; em dev local usa default ou env).

Cada teste é envolto em transação que dá rollback no teardown — DB volta
limpo entre testes, sem precisar drop/create schema.
"""
from __future__ import annotations

import asyncio
import os
import socket
from typing import AsyncIterator

import pytest
import asyncpg


def _test_database_url() -> str:
    """Resolve URL do Postgres pros testes de integração.

    Prioridade:
    1. TEST_DATABASE_URL (CI define)
    2. DATABASE_URL (dev local — mesma DB do app, perigoso!)
    3. fallback localhost padrão
    """
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://agente:agente@localhost:5432/agente_inteligencia"
    )


def _postgres_reachable(url: str, timeout: float = 1.0) -> bool:
    """Probe rápido pra ver se Postgres responde no host:port. Não tenta
    autenticar — só socket. Mantém o pytest fora do fluxo se não tem DB."""
    try:
        # Parse simples — `postgresql://user:pass@host:port/db`
        without_scheme = url.split("://", 1)[1]
        auth_host = without_scheme.split("/", 1)[0]
        host_port = auth_host.split("@")[-1]
        if ":" in host_port:
            host, port_str = host_port.split(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 5432
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        finally:
            s.close()
    except Exception:
        return False


@pytest.fixture(scope="session")
def event_loop():
    """Mesmo loop pra sessão inteira — fixtures async session-scoped funcionam."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_postgres():
    """Pula TODOS os testes do diretório integration/ se Postgres inacessível.

    Mantém o CI funcional mesmo sem service container e dev local sem docker —
    testes simplesmente não rodam (skip), sem falhar.
    """
    url = _test_database_url()
    if not _postgres_reachable(url):
        pytest.skip(
            f"Postgres não acessível em {url} — testes de integração pulados. "
            "Rode `docker compose up -d postgres` ou setar TEST_DATABASE_URL.",
            allow_module_level=True,
        )


@pytest.fixture(scope="session")
async def db_pool() -> AsyncIterator[asyncpg.Pool]:
    """Pool dedicado pros testes (não reusa app pool — evita interferência).

    Aplica o schema do app (CREATE TABLEs + migrations) na primeira vez —
    idempotente, então safe se DB já estava configurada.
    """
    url = _test_database_url()
    pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=3, command_timeout=30)
    # Importa e aplica schema. init_pool_connection registra codec pgvector
    # (no-op se lib não instalada — testes que precisam de vector vão skip).
    from app.core.database import SCHEMA, _IDEMPOTENT_MIGRATIONS, _split_sql

    async with pool.acquire() as con:
        for stmt in _split_sql(SCHEMA):
            try:
                await con.execute(stmt)
            except Exception:
                pass  # idempotente; ignora se já existe
        for migration in _IDEMPOTENT_MIGRATIONS:
            try:
                await con.execute(migration)
            except Exception:
                pass
    yield pool
    await pool.close()


@pytest.fixture
async def db_tx(db_pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Connection envolvida em transação que dá ROLLBACK no teardown.

    Mantém DB limpo entre testes sem precisar truncate/recreate. Cada teste
    vê estado virgem do esquema + dados que ele mesmo inseriu.
    """
    async with db_pool.acquire() as con:
        tx = con.transaction()
        await tx.start()
        try:
            yield con
        finally:
            await tx.rollback()
