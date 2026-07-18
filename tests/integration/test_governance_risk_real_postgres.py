"""Registro de risco (governance_risk) contra Postgres real.

A tabela nova só existe se o SCHEMA foi aplicado no boot. INSERT com as colunas
+ a UNIQUE(entity_type, entity_id) só se validam contra Postgres de verdade.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestGovernanceRiskTable:
    @pytest.mark.asyncio
    async def test_insert_classificacao(self, db_tx):
        rid = str(uuid.uuid4())
        eid = f"agent-{rid[:8]}"
        await db_tx.execute(
            "INSERT INTO governance_risk (id, entity_type, entity_id, tier, rationale, mitigations, classified_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            rid, "agent", eid, "high", "decisão consequente", "revisão humana", "gov",
        )
        row = await db_tx.fetchrow("SELECT tier, entity_type, classified_by FROM governance_risk WHERE id=$1", rid)
        assert row["tier"] == "high"
        assert row["entity_type"] == "agent"
        assert row["classified_by"] == "gov"

    @pytest.mark.asyncio
    async def test_unique_por_ativo(self, db_tx):
        import asyncpg
        eid = f"agent-{uuid.uuid4().hex[:8]}"
        await db_tx.execute(
            "INSERT INTO governance_risk (id, entity_type, entity_id, tier) VALUES ($1,$2,$3,$4)",
            str(uuid.uuid4()), "agent", eid, "minimal",
        )
        # segunda classificação do MESMO ativo viola a UNIQUE
        with pytest.raises(asyncpg.UniqueViolationError):
            await db_tx.execute(
                "INSERT INTO governance_risk (id, entity_type, entity_id, tier) VALUES ($1,$2,$3,$4)",
                str(uuid.uuid4()), "agent", eid, "high",
            )
