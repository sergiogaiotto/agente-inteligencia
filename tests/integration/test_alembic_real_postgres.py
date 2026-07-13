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

    # 1ª execução — cria alembic_version + aplica a cadeia até o HEAD atual
    # (0001_baseline no-op → 0002_verifications_gold_case_id).
    await asyncio.to_thread(_alembic_upgrade_sync)
    async with db_pool.acquire() as con:
        version = await con.fetchval("SELECT version_num FROM alembic_version")
    assert version == "0003_interactions_owner_user_id"

    # Idempotente — 2ª execução não muda nada (DB já em head).
    await asyncio.to_thread(_alembic_upgrade_sync)
    async with db_pool.acquire() as con:
        n = await con.fetchval("SELECT count(*) FROM alembic_version")
    assert n == 1


async def test_gold_case_id_persiste_e_link_por_interaction(db_tx):
    """Keystone 33.10.0: a coluna existe no Postgres real (via DDL base) e o
    UPDATE de ligação do harness (por interaction_id, só quando NULL) funciona."""
    # Persiste um valor direto.
    await db_tx.execute(
        "INSERT INTO verifications (id, interaction_id, gold_case_id) VALUES ($1,$2,$3)",
        "v-rt-1", "int-rt-1", "gc-rt-1",
    )
    assert await db_tx.fetchval(
        "SELECT gold_case_id FROM verifications WHERE id=$1", "v-rt-1"
    ) == "gc-rt-1"

    # Link do harness: preenche a linha cuja gold_case_id está NULL.
    await db_tx.execute(
        "INSERT INTO verifications (id, interaction_id) VALUES ($1,$2)", "v-rt-2", "int-rt-2",
    )
    await db_tx.execute(
        "UPDATE verifications SET gold_case_id=$1 "
        "WHERE interaction_id=$2 AND gold_case_id IS NULL", "gc-rt-2", "int-rt-2",
    )
    assert await db_tx.fetchval(
        "SELECT gold_case_id FROM verifications WHERE id=$1", "v-rt-2"
    ) == "gc-rt-2"

    # NÃO sobrescreve um elo já gravado (guarda IS NULL).
    await db_tx.execute(
        "UPDATE verifications SET gold_case_id=$1 "
        "WHERE interaction_id=$2 AND gold_case_id IS NULL", "SOBRESCRITO", "int-rt-1",
    )
    assert await db_tx.fetchval(
        "SELECT gold_case_id FROM verifications WHERE id=$1", "v-rt-1"
    ) == "gc-rt-1"
