"""Integração (Postgres real) — Alembic upgrade (Onda 4, 33.6.0).

Prova que ``_alembic_upgrade_sync`` (o que init_db chama em thread) cria a
alembic_version e carimba o baseline, e que é idempotente (rodar 2× não duplica).
Usa ALEMBIC_DATABASE_URL p/ mirar o DB de teste (o alembic normalmente lê
settings.database_url; o CI usa TEST_DATABASE_URL).
"""
from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_alembic_upgrade_carimba_baseline(db_pool, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", url)

    from app.core.database import _alembic_upgrade_sync

    # 1ª execução — cria alembic_version + registra o baseline.
    await asyncio.to_thread(_alembic_upgrade_sync)
    async with db_pool.acquire() as con:
        version = await con.fetchval("SELECT version_num FROM alembic_version")
    assert version == "0001_baseline"

    # Idempotente — 2ª execução não muda nada (DB já em head).
    await asyncio.to_thread(_alembic_upgrade_sync)
    async with db_pool.acquire() as con:
        n = await con.fetchval("SELECT count(*) FROM alembic_version")
    assert n == 1
