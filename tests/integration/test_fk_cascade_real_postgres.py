"""Integração (Postgres real) — FK ON DELETE CASCADE no núcleo (33.5.0).

Prova, contra Postgres de verdade (mocks escondem FK):
- deletar uma interaction CASCATEIA turns/tool_calls/binding_executions;
- verifications NÃO é cascateada (auditoria do juiz preservada de propósito);
- a FK REJEITA filho com interaction_id inexistente;
- a receita de migração LIMPA órfãos ANTES do ADD CONSTRAINT (teste em DB sujo).

Usa a fixture db_tx (conexão em transação com ROLLBACK no teardown) — DDL em
Postgres é transacional, então DROP/ADD CONSTRAINT também revertem, sem poluir.
"""
from __future__ import annotations

import uuid

import asyncpg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _mk_interaction(con) -> str:
    iid = str(uuid.uuid4())
    await con.execute("INSERT INTO interactions (id) VALUES ($1)", iid)
    return iid


class TestFkCascade:
    async def test_delete_interaction_cascateia_filhos(self, db_tx):
        con = db_tx
        iid = await _mk_interaction(con)
        await con.execute(
            "INSERT INTO turns (id, interaction_id) VALUES ($1, $2)",
            str(uuid.uuid4()), iid)
        await con.execute(
            "INSERT INTO tool_calls (id, interaction_id) VALUES ($1, $2)",
            str(uuid.uuid4()), iid)
        await con.execute(
            "INSERT INTO binding_executions (id, interaction_id, binding_id) VALUES ($1, $2, $3)",
            str(uuid.uuid4()), iid, "b1")

        assert await con.fetchval(
            "SELECT count(*) FROM turns WHERE interaction_id=$1", iid) == 1

        # DELETE do pai → a FK ON DELETE CASCADE apaga os 3 filhos no banco.
        await con.execute("DELETE FROM interactions WHERE id=$1", iid)

        assert await con.fetchval(
            "SELECT count(*) FROM turns WHERE interaction_id=$1", iid) == 0
        assert await con.fetchval(
            "SELECT count(*) FROM tool_calls WHERE interaction_id=$1", iid) == 0
        assert await con.fetchval(
            "SELECT count(*) FROM binding_executions WHERE interaction_id=$1", iid) == 0

    async def test_verifications_nao_cascateia(self, db_tx):
        # verifications NÃO tem FK CASCADE: a auditoria do juiz deve SOBREVIVER ao
        # delete do pai (na consolidação ela é re-apontada ao master em Python).
        con = db_tx
        iid = await _mk_interaction(con)
        vid = str(uuid.uuid4())
        await con.execute(
            "INSERT INTO verifications (id, interaction_id) VALUES ($1, $2)", vid, iid)

        await con.execute("DELETE FROM interactions WHERE id=$1", iid)

        assert await con.fetchval(
            "SELECT count(*) FROM verifications WHERE id=$1", vid) == 1

    async def test_fk_rejeita_filho_com_pai_inexistente(self, db_tx):
        con = db_tx
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await con.execute(
                "INSERT INTO turns (id, interaction_id) VALUES ($1, $2)",
                str(uuid.uuid4()), "no-such-interaction")


class TestOrphanCleanupMigration:
    async def test_migracao_limpa_orfaos_antes_do_add_constraint(self, db_tx):
        """DB SUJO: dropa a FK, planta um turn órfão, re-aplica as MESMAS 3
        entradas da migração (DELETE órfãos → DROP → ADD) e prova que o órfão
        sumiu E a FK voltou. Sem o DELETE, o ADD CONSTRAINT falharia (é o footgun
        que a migração previne)."""
        con = db_tx
        await con.execute(
            "ALTER TABLE turns DROP CONSTRAINT IF EXISTS turns_interaction_id_fkey")
        orphan = str(uuid.uuid4())
        await con.execute(
            "INSERT INTO turns (id, interaction_id) VALUES ($1, $2)",
            orphan, "ghost-interaction")

        # Receita da migração (idêntica a _IDEMPOTENT_MIGRATIONS):
        await con.execute(
            "DELETE FROM turns WHERE interaction_id IS NOT NULL "
            "AND interaction_id NOT IN (SELECT id FROM interactions)")
        await con.execute(
            "ALTER TABLE turns DROP CONSTRAINT IF EXISTS turns_interaction_id_fkey")
        await con.execute(
            "ALTER TABLE turns ADD CONSTRAINT turns_interaction_id_fkey "
            "FOREIGN KEY (interaction_id) REFERENCES interactions(id) ON DELETE CASCADE")

        # Órfão limpo + FK de volta (novo órfão é rejeitado).
        assert await con.fetchval(
            "SELECT count(*) FROM turns WHERE id=$1", orphan) == 0
        with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
            await con.execute(
                "INSERT INTO turns (id, interaction_id) VALUES ($1, $2)",
                str(uuid.uuid4()), "still-ghost")
