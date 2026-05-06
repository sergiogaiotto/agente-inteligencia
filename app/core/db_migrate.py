"""Migração de dados SQLite → PostgreSQL (one-shot).

Uso (com .env carregado):
    python -m app.core.db_migrate                 # usa data/agente_inteligencia.db
    python -m app.core.db_migrate ./outro.db      # caminho custom

O script:
1. Garante o schema Postgres atualizado (chama init_db)
2. Lê cada tabela conhecida do SQLite legado
3. Faz INSERT ... ON CONFLICT (id) DO NOTHING no Postgres
4. Reporta totais e erros por tabela

Idempotente: pode ser executado múltiplas vezes sem duplicar.
audit_log é tratado especialmente (id é BIGSERIAL no Postgres).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import asyncpg

from app.core.config import get_settings
from app.core.database import init_db, close_db, _get_pool


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("db_migrate")


# Ordem topológica (parents antes de children por causa de FKs)
TABLES_ORDER = [
    "skills",
    "agents",
    "agent_bindings",
    "mesh_connections",
    "envelopes",
    "journeys",
    "interactions",
    "turns",
    "knowledge_sources",
    "evidences",
    "tools",
    "tool_calls",
    "traces",
    "releases",
    "gold_cases",
    "eval_runs",
    "car_entries",
    "drift_events",
    "platform_settings",
    "system_prompts",
    "users",
    "domains",
    "api_connectors",
    "api_endpoints",
    "api_call_logs",
    # audit_log é BIGSERIAL — tratado à parte
    "audit_log",
]


def _coerce_timestamp(value: Any) -> Any:
    """SQLite armazena timestamps como string ISO. asyncpg aceita string em
    colunas TIMESTAMP, mas tropeça com formatos não-padrão. Convertemos
    para datetime quando possível, deixamos como veio em caso contrário."""
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return None
    # tenta ISO 8601 (com ou sem 'T')
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s  # asyncpg vai tentar parsear


async def _pg_table_columns(con: asyncpg.Connection, table: str) -> dict[str, str]:
    rows = await con.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=$1",
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


async def _migrate_table(
    sqlite_db: aiosqlite.Connection,
    pg_pool: asyncpg.Pool,
    table: str,
) -> tuple[int, int, int]:
    """Migra uma tabela. Retorna (lidas, inseridas, erros)."""
    # Verifica se a tabela existe no SQLite
    cur = await sqlite_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    if not await cur.fetchone():
        logger.info(f"⊘ {table}: ausente no SQLite — pulando")
        return (0, 0, 0)

    # Lê todas as linhas
    sqlite_db.row_factory = aiosqlite.Row
    cur = await sqlite_db.execute(f"SELECT * FROM {table}")
    rows = await cur.fetchall()
    total = len(rows)
    if total == 0:
        logger.info(f"∅ {table}: vazia no SQLite")
        return (0, 0, 0)

    inserted = 0
    errors = 0
    timestamp_types = {"timestamp without time zone", "timestamp with time zone", "timestamp"}

    async with pg_pool.acquire() as con:
        pg_cols = await _pg_table_columns(con, table)
        if not pg_cols:
            logger.warning(f"⚠ {table}: tabela inexistente no Postgres — pulando")
            return (total, 0, total)

        for row in rows:
            sqlite_dict = dict(row)
            # Filtra colunas que não existem no Postgres
            data = {k: v for k, v in sqlite_dict.items() if k in pg_cols}
            # audit_log: omite id (BIGSERIAL gera automaticamente)
            if table == "audit_log":
                data.pop("id", None)
            # Coerção de timestamps
            for col, val in list(data.items()):
                if pg_cols.get(col) in timestamp_types:
                    data[col] = _coerce_timestamp(val)

            if not data:
                continue

            keys = list(data.keys())
            cols_sql = ", ".join(keys)
            phs = ", ".join(f"${i+1}" for i in range(len(keys)))
            values = [data[k] for k in keys]

            if table == "platform_settings":
                conflict = "ON CONFLICT (key) DO NOTHING"
            elif table == "audit_log":
                conflict = ""  # id é gerado
            elif "id" in pg_cols:
                conflict = "ON CONFLICT (id) DO NOTHING"
            else:
                conflict = ""

            sql = f"INSERT INTO {table} ({cols_sql}) VALUES ({phs}) {conflict}"
            try:
                await con.execute(sql, *values)
                inserted += 1
            except Exception as e:
                errors += 1
                if errors <= 3:
                    logger.warning(f"  ↳ {table}: erro em linha id={sqlite_dict.get('id')} — {type(e).__name__}: {e}")

    logger.info(f"✓ {table}: lidas={total} inseridas={inserted} erros={errors}")
    return (total, inserted, errors)


async def main(sqlite_path: str | None = None):
    sqlite_file = Path(sqlite_path) if sqlite_path else Path("data/agente_inteligencia.db")
    if not sqlite_file.exists():
        logger.error(f"Arquivo SQLite não encontrado: {sqlite_file.resolve()}")
        return 1

    settings = get_settings()
    logger.info(f"Origem (SQLite): {sqlite_file.resolve()}")
    logger.info(f"Destino (Postgres): {settings.database_url}")

    logger.info("→ Inicializando schema Postgres…")
    await init_db()

    pg_pool = _get_pool()
    sqlite_db = await aiosqlite.connect(str(sqlite_file))

    summary = {}
    try:
        for table in TABLES_ORDER:
            try:
                summary[table] = await _migrate_table(sqlite_db, pg_pool, table)
            except Exception as e:
                logger.error(f"✗ {table}: falha grave — {type(e).__name__}: {e}")
                summary[table] = (0, 0, -1)
    finally:
        await sqlite_db.close()
        await close_db()

    total_read = sum(s[0] for s in summary.values())
    total_ins = sum(s[1] for s in summary.values())
    total_err = sum(max(0, s[2]) for s in summary.values())
    logger.info("─" * 60)
    logger.info(f"TOTAL — lidas: {total_read} · inseridas: {total_ins} · erros: {total_err}")
    return 0 if total_err == 0 else 2


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    rc = asyncio.run(main(arg))
    sys.exit(rc)
