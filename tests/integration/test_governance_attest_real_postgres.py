"""Attestation + papéis (governance_officer / governance_attestation) — Postgres real.

As tabelas novas só existem se o SCHEMA foi aplicado no boot; o INSERT com as
colunas e a UNIQUE(office, user_id) só se validam contra Postgres de verdade.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestGovernanceAttestTables:
    @pytest.mark.asyncio
    async def test_officer_insert_e_unique(self, db_tx):
        import asyncpg
        uid = f"u-{uuid.uuid4().hex[:8]}"
        await db_tx.execute(
            "INSERT INTO governance_officer (id, office, user_id, assigned_by) VALUES ($1,$2,$3,$4)",
            str(uuid.uuid4()), "dpo", uid, "gov",
        )
        row = await db_tx.fetchrow("SELECT office FROM governance_officer WHERE user_id=$1", uid)
        assert row["office"] == "dpo"
        # mesmo (office, user_id) viola a UNIQUE
        with pytest.raises(asyncpg.UniqueViolationError):
            await db_tx.execute(
                "INSERT INTO governance_officer (id, office, user_id) VALUES ($1,$2,$3)",
                str(uuid.uuid4()), "dpo", uid,
            )

    @pytest.mark.asyncio
    async def test_attestation_insert(self, db_tx):
        aid = str(uuid.uuid4())
        await db_tx.execute(
            "INSERT INTO governance_attestation (id, scope, entity_id, statement, signed_by) "
            "VALUES ($1,$2,$3,$4,$5)",
            aid, "platform", None, "pronto para produção", "gov",
        )
        row = await db_tx.fetchrow("SELECT scope, statement, signed_by FROM governance_attestation WHERE id=$1", aid)
        assert row["scope"] == "platform"
        assert row["statement"] == "pronto para produção"
        assert row["signed_by"] == "gov"
