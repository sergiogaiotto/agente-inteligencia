"""Integração (Postgres real) — coerção de write do Repository (33.6.1).

Prova que Repository.create/update escrevem um dict num JSONB e um datetime
tz-aware num TIMESTAMP SEM o 500 do asyncpg (o footgun que mocks escondem). Usa
uma tabela-sonda em public (a introspecção de tipos lê information_schema).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_create_coage_jsonb_e_timestamp(db_pool, monkeypatch):
    from app.core import database
    from app.core.database import Repository

    # Repository usa _get_pool() (o pool do app) — aponta p/ o pool de teste.
    monkeypatch.setattr(database, "_pool", db_pool)

    async with db_pool.acquire() as con:
        await con.execute(
            "CREATE TABLE IF NOT EXISTS _coerce_probe "
            "(id TEXT PRIMARY KEY, blob JSONB, ts TIMESTAMP)")
        await con.execute("DELETE FROM _coerce_probe")
    database._COL_TYPE_CACHE.pop("_coerce_probe", None)

    repo = Repository("_coerce_probe")
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    # dict cru no JSONB + datetime aware no TIMESTAMP — sem json.dumps/strip manual.
    await repo.create({"id": "p1", "blob": {"x": [1, 2], "y": "z"}, "ts": aware})

    try:
        async with db_pool.acquire() as con:
            row = await con.fetchrow("SELECT blob, ts FROM _coerce_probe WHERE id='p1'")
    finally:
        async with db_pool.acquire() as con:
            await con.execute("DROP TABLE IF EXISTS _coerce_probe")

    # blob persistiu como JSON válido (asyncpg sem codec devolve str) e o ts é naive.
    blob = row["blob"]
    parsed = json.loads(blob) if isinstance(blob, str) else blob
    assert parsed == {"x": [1, 2], "y": "z"}
    assert row["ts"] == datetime(2026, 1, 1, 12, 0)
    assert row["ts"].tzinfo is None
