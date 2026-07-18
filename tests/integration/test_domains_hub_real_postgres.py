"""Hub de Domínios contra Postgres real — pega o que mock não pega.

As colunas aditivas (owner_user_id/color/icon/status) só existem se as
migrações idempotentes rodaram. Um INSERT com elas estoura UndefinedColumn
se a migração falhou — exatamente a classe de bug que a suíte mockada perde.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestDomainColumns:
    @pytest.mark.asyncio
    async def test_insert_com_colunas_novas(self, db_tx):
        did = str(uuid.uuid4())
        name = f"dom-{did[:8]}"
        # Se qualquer coluna não existir na DDL/migração, asyncpg estoura aqui.
        await db_tx.execute(
            "INSERT INTO domains (id, name, description, owner_user_id, color, icon, status) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            did, name, "desc", None, "#378ADD", None, "active",
        )
        row = await db_tx.fetchrow(
            "SELECT name, color, status FROM domains WHERE id=$1", did
        )
        assert row["name"] == name
        assert row["color"] == "#378ADD"
        assert row["status"] == "active"

    @pytest.mark.asyncio
    async def test_status_default_active(self, db_tx):
        # Domínio legado (só nome) recebe status default 'active' pela migração.
        did = str(uuid.uuid4())
        await db_tx.execute(
            "INSERT INTO domains (id, name) VALUES ($1,$2)", did, f"legacy-{did[:8]}"
        )
        status = await db_tx.fetchval("SELECT status FROM domains WHERE id=$1", did)
        assert status == "active"
