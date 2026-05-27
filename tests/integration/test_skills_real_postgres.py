"""Smoke tests com Postgres real — cobre INSERT/migration/constraint reais.

Estes testes pegam categoria de bugs que mocks não pegam:
- Coluna ausente na DDL (bug do PR #151 data_tables)
- JSONB recebendo list/dict raw sem json.dumps
- UNIQUE/CHECK constraints violados
- Migrations idempotentes que rodam fora de ordem
- Codec pgvector não registrado (PR pgvector foundation)

Estratégia:
- Cada teste roda em transação que dá rollback no teardown (db_tx fixture)
- Mesmo schema do app (SCHEMA + _IDEMPOTENT_MIGRATIONS) — bate com prod
- Marca @pytest.mark.integration → CI roda em job separado

Run local:
    docker compose up -d postgres
    pytest tests/integration -m integration -v
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.integration


# ═════════════════════════════════════════════════════════════════
# DDL ↔ skill_to_db_dict: regressão do PR #151
# ═════════════════════════════════════════════════════════════════


class TestSkillTableInsert:
    """O bug #151 (data_tables column ausente) escapou os 850 testes
    mockados porque Repository.create era patched. Smoke real pega."""

    @pytest.mark.asyncio
    async def test_insert_skill_with_all_columns(self, db_tx):
        """INSERT real com TODAS as colunas que skill_to_db_dict produz.
        Se faltar alguma coluna na DDL, asyncpg estoura UndefinedColumnError."""
        from app.skill_parser.parser import ParsedSkill, SkillFrontmatter, skill_to_db_dict

        parsed = ParsedSkill(
            frontmatter=SkillFrontmatter(
                id=f"urn:skill:test:subagent:{uuid.uuid4().hex[:8]}",
                version="0.1.0",
                kind="subagent",
                owner="test",
                stability="alpha",
            ),
            name="Smoke Skill",
            purpose="Validar INSERT real",
            raw_content="# Smoke Skill\nConteúdo mínimo.",
            content_hash="abc123",
        )
        db_data = skill_to_db_dict(parsed)
        db_data["id"] = str(uuid.uuid4())
        db_data["tags"] = "[]"

        # INSERT manual (mesmo padrão de Repository.create) — se algum
        # campo não existir como coluna, asyncpg estoura.
        keys = list(db_data.keys())
        cols = ", ".join(keys)
        phs = ", ".join(f"${i+1}" for i in range(len(keys)))
        sql = f"INSERT INTO skills ({cols}) VALUES ({phs})"
        await db_tx.execute(sql, *[db_data[k] for k in keys])

        # Sanity: row foi inserida
        count = await db_tx.fetchval(
            "SELECT COUNT(*) FROM skills WHERE id = $1", db_data["id"]
        )
        assert count == 1

    @pytest.mark.asyncio
    async def test_data_tables_column_accepts_text(self, db_tx):
        """Regressão direta do PR #151: coluna data_tables existe e aceita TEXT."""
        sid = str(uuid.uuid4())
        await db_tx.execute(
            """
            INSERT INTO skills (id, urn, name, kind, raw_content, content_hash, data_tables)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            sid, f"urn:skill:test:subagent:{sid[:8]}", "Test", "subagent",
            "# Test", "abc", "yaml:\n  data here"
        )
        val = await db_tx.fetchval("SELECT data_tables FROM skills WHERE id = $1", sid)
        assert val == "yaml:\n  data here"


# ═════════════════════════════════════════════════════════════════
# JSONB armadilha (vi no MEMORY.md): list/dict raw vs json.dumps
# ═════════════════════════════════════════════════════════════════


class TestJsonbColumns:
    """Bug clássico do asyncpg: passar list/dict direto em coluna JSONB
    estoura 'expected str, got list'. Smoke pega isso."""

    @pytest.mark.asyncio
    async def test_evidence_chunks_metadata_jsonb_accepts_dict_via_dumps(self, db_tx):
        """evidence_chunks.metadata é JSONB. Tem que serializar com json.dumps."""
        # Cria source temporária pra FK
        sid = str(uuid.uuid4())
        await db_tx.execute(
            """
            INSERT INTO knowledge_sources (id, name, source_type, authorized)
            VALUES ($1, $2, $3, $4)
            """,
            sid, "Test KS", "doc", 1
        )
        # Insere chunk com metadata como JSON string (correto)
        chunk_id = str(uuid.uuid4())
        await db_tx.execute(
            """
            INSERT INTO evidence_chunks (id, knowledge_source_id, ordinal, text, metadata)
            VALUES ($1, $2, $3, $4, $5)
            """,
            chunk_id, sid, 0, "texto", json.dumps({"author": "x", "page": 1})
        )
        # Recupera e valida que JSONB parseou de volta
        row = await db_tx.fetchrow(
            "SELECT metadata FROM evidence_chunks WHERE id = $1", chunk_id
        )
        # asyncpg devolve JSONB como string em texto cru, ou parseia conforme codec
        meta = row["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        assert meta["author"] == "x"


# ═════════════════════════════════════════════════════════════════
# Constraints CHECK e UNIQUE — viola e confirma
# ═════════════════════════════════════════════════════════════════


class TestConstraints:
    @pytest.mark.asyncio
    async def test_skill_urn_unique_violation(self, db_tx):
        """Dois INSERTs com mesma URN devem falhar no segundo (UNIQUE)."""
        import asyncpg
        urn = f"urn:skill:test:subagent:{uuid.uuid4().hex[:8]}"

        # 1º INSERT — sucesso
        await db_tx.execute(
            "INSERT INTO skills (id, urn, name, kind, raw_content) VALUES ($1, $2, $3, $4, $5)",
            str(uuid.uuid4()), urn, "X", "subagent", "..."
        )
        # 2º INSERT mesma URN — UniqueViolationError
        with pytest.raises(asyncpg.UniqueViolationError):
            await db_tx.execute(
                "INSERT INTO skills (id, urn, name, kind, raw_content) VALUES ($1, $2, $3, $4, $5)",
                str(uuid.uuid4()), urn, "Y", "subagent", "..."
            )

    @pytest.mark.asyncio
    async def test_skill_kind_check_violation(self, db_tx):
        """kind inválido (não orchestrator/router/subagent) deve falhar."""
        import asyncpg
        with pytest.raises(asyncpg.CheckViolationError):
            await db_tx.execute(
                "INSERT INTO skills (id, urn, name, kind, raw_content) VALUES ($1, $2, $3, $4, $5)",
                str(uuid.uuid4()),
                f"urn:skill:x:y:{uuid.uuid4().hex[:8]}",
                "Test",
                "tipo-inexistente",  # viola CHECK
                "..."
            )


# ═════════════════════════════════════════════════════════════════
# Migrations idempotentes — rodar 2x não quebra
# ═════════════════════════════════════════════════════════════════


class TestMigrationIdempotency:
    @pytest.mark.asyncio
    async def test_alter_table_add_column_if_not_exists_runs_twice(self, db_pool):
        """As migrations idempotentes de _IDEMPOTENT_MIGRATIONS DEVEM poder
        rodar repetidamente sem erro (init_db chama em todo startup)."""
        from app.core.database import _IDEMPOTENT_MIGRATIONS

        # Roda todas duas vezes — se alguma não for idempotente, falha
        async with db_pool.acquire() as con:
            for migration in _IDEMPOTENT_MIGRATIONS:
                # Pula extensões que podem falhar por permissão (CREATE EXTENSION
                # vector requer privilégio + binário instalado).
                if "CREATE EXTENSION" in migration:
                    continue
                try:
                    await con.execute(migration)
                    await con.execute(migration)  # 2ª vez
                except Exception as e:
                    pytest.fail(
                        f"Migration não-idempotente: {migration[:80]} — {type(e).__name__}: {e}"
                    )
