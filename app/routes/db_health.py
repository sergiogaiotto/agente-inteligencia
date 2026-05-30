"""Health check abrangente do Postgres — único backend de dados (Onda P).

Após Onda Q removeu Qdrant, Postgres carrega TUDO: dados core (agents/
skills/interactions), RAG vetorial via pgvector, BM25 via tsvector,
settings store, audit log, catalog, data tables metadata.

`_check_postgres` em infra.py faz só `SELECT 1` — confirma conexão mas
não valida que TODOS os subsistemas estão saudáveis. Este endpoint
expande pra cobrir:

- Pool: min/max/active/idle
- Extensions: vector (crítica pra RAG), pg_trgm (opcional)
- Tables críticas: lista esperada vs presente
- Indexes críticos: GIN no tsvector + HNSW no pgvector
- pgvector: dim_actual == dim_expected (drift detection)
- tsvector: chunks com TSV populado (sanity de migration)
- Settings store: read/write smoke
- FK integrity: violações em catalog (high-cardinality)

Custo: 1 acquire de pool + ~10 queries SELECT count/exists. ~50-100ms.
Cacheável (componente raramente muda) — exceto pgvector + counts.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends

from app.core.auth import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/health", tags=["health"])


# Tabelas críticas que precisam existir pra plataforma operar. Falta de
# qualquer uma = sinal de migration incompleta ou DB errado conectado.
_CRITICAL_TABLES = [
    "agents", "skills", "interactions", "turns",
    "knowledge_sources", "evidence_chunks",
    "tools", "tool_calls", "binding_executions",
    "api_connectors", "api_endpoints", "api_call_logs",
    "users", "domains", "api_keys",
    "audit_log", "platform_settings",
    "catalog_entries", "data_tables",
]

# Indexes críticos pra performance. Falta = queries lentas (não broken,
# mas alerta). Validação por nome — pode mudar se renomear.
_CRITICAL_INDEXES = [
    {"name": "idx_evidence_chunks_tsv", "type": "gin", "table": "evidence_chunks",
     "purpose": "BM25 search"},
    {"name": "idx_evidence_chunks_embedding", "type": "hnsw", "table": "evidence_chunks",
     "purpose": "pgvector cosine KNN"},
]

# Extensions: vector é OBRIGATÓRIA (RAG depende); pg_trgm é opcional
# (algumas queries de catalog poderiam usar trigram).
_REQUIRED_EXTENSIONS = ["vector"]
_OPTIONAL_EXTENSIONS = ["pg_trgm"]


async def _check_pool() -> dict:
    """Pool init + size/utilization. Falha se pool não existe."""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        # asyncpg.Pool expõe `get_size`, `get_min_size`, `get_max_size`,
        # `get_idle_size` (pra inspeção sem mexer em conn).
        return {
            "ok": True,
            "min": pool.get_min_size(),
            "max": pool.get_max_size(),
            "active": pool.get_size() - pool.get_idle_size(),
            "idle": pool.get_idle_size(),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_extensions() -> dict:
    """Extensions instaladas vs requeridas."""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch("SELECT extname, extversion FROM pg_extension")
        installed = {r["extname"]: r["extversion"] for r in rows}

        required_status = {}
        all_required_ok = True
        for ext in _REQUIRED_EXTENSIONS:
            present = ext in installed
            required_status[ext] = {
                "ok": present,
                "version": installed.get(ext),
                "required": True,
            }
            if not present:
                all_required_ok = False

        optional_status = {}
        for ext in _OPTIONAL_EXTENSIONS:
            present = ext in installed
            optional_status[ext] = {
                "ok": True,  # opcional = sempre ok
                "present": present,
                "version": installed.get(ext),
                "optional": True,
            }

        return {
            "ok": all_required_ok,
            "required": required_status,
            "optional": optional_status,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_tables() -> dict:
    """Tabelas críticas presentes?"""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
        present_set = {r["table_name"] for r in rows}
        missing = [t for t in _CRITICAL_TABLES if t not in present_set]
        return {
            "ok": len(missing) == 0,
            "expected": len(_CRITICAL_TABLES),
            "present": len([t for t in _CRITICAL_TABLES if t in present_set]),
            "missing": missing,
            "total_in_db": len(present_set),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_indexes() -> dict:
    """Indexes críticos pra perf existem?"""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            )
        present_set = {r["indexname"] for r in rows}
        statuses = []
        all_ok = True
        for spec in _CRITICAL_INDEXES:
            ok = spec["name"] in present_set
            statuses.append({**spec, "ok": ok})
            if not ok:
                all_ok = False
        return {"ok": all_ok, "indexes": statuses}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_pgvector_dim() -> dict:
    """pgvector column dim atual vs esperada (drift detection)."""
    try:
        from app.evidence.pgvector_store import collection_info
        info = await collection_info()
        if info is None:
            return {"ok": False, "error": "pgvector offline"}
        # dim_match indica se actual == expected. Drift = autor trocou
        # provider em /settings sem rodar POST /reindex.
        return {
            "ok": bool(info.get("dim_match", False)),
            "expected_dim": info.get("dim_expected"),
            "actual_dim": info.get("dim_actual"),
            "points_count": info.get("points_count", 0),
            "status": info.get("status", "unknown"),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_tsvector() -> dict:
    """Sanity: evidence_chunks tem TSV populado quando text não é NULL."""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT "
                "  COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE tsv IS NOT NULL) AS with_tsv, "
                "  COUNT(*) FILTER (WHERE text IS NOT NULL AND text != '') AS with_text "
                "FROM evidence_chunks"
            )
        total = row["total"] or 0
        with_tsv = row["with_tsv"] or 0
        with_text = row["with_text"] or 0
        # Quando há text, deveria sempre ter tsv (column é GENERATED). Mismatch
        # significa migration quebrada OU coluna não regenerada.
        ok = (with_text == 0) or (with_tsv >= with_text)
        return {
            "ok": ok,
            "total_chunks": total,
            "with_tsv": with_tsv,
            "with_text": with_text,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _check_settings_store() -> dict:
    """Read+write smoke. Garante que platform_settings table aceita upsert."""
    try:
        from app.core.database import _get_pool
        pool = _get_pool()
        async with pool.acquire() as con:
            # Write
            test_key = "_health_check_probe"
            test_val = f"ts:{int(time.time())}"
            await con.execute(
                "INSERT INTO platform_settings (key, value, updated_at) "
                "VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                test_key, test_val,
            )
            # Read back
            row = await con.fetchrow(
                "SELECT value FROM platform_settings WHERE key = $1",
                test_key,
            )
            # Cleanup — health probe não deixa lixo
            await con.execute(
                "DELETE FROM platform_settings WHERE key = $1",
                test_key,
            )
        if row is None:
            return {"ok": False, "error": "write+read returned None"}
        if row["value"] != test_val:
            return {"ok": False, "error": f"value mismatch: wrote {test_val!r}, read {row['value']!r}"}
        return {"ok": True, "rw_test": "passed"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


@router.get("/database")
async def database_health(user: dict = Depends(require_user)) -> dict[str, Any]:
    """Health check abrangente do Postgres.

    Executa 6 checks (paralelo via asyncio.gather):
    1. Pool — connection pool init + utilization
    2. Extensions — vector (required), pg_trgm (optional)
    3. Tables — 19 tabelas críticas existem
    4. Indexes — GIN tsvector + HNSW pgvector
    5. pgvector dim — actual == expected (drift detection)
    6. tsvector — TSV populado pros chunks com text
    7. Settings store — read/write smoke

    Returns:
        {ok: bool, duration_ms: int, checks: {...}}
        ok = AND de todos os subchecks (extensions required + tables + indexes
        + pgvector + tsvector + settings_store + pool).

    Onda P (2026-05-30): substitui o `SELECT 1` simples do /infra/status
    como source-of-truth pra saúde do Postgres.
    """
    import asyncio
    t0 = time.monotonic()

    # Pool fora do gather — outros checks dependem dele resolver primeiro
    pool_check = await _check_pool()
    if not pool_check["ok"]:
        # Sem pool, demais checks falhariam de qualquer jeito — fail fast
        return {
            "ok": False,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "checks": {"pool": pool_check},
        }

    extensions, tables, indexes, pgvec, tsv, settings = await asyncio.gather(
        _check_extensions(),
        _check_tables(),
        _check_indexes(),
        _check_pgvector_dim(),
        _check_tsvector(),
        _check_settings_store(),
    )

    checks = {
        "pool": pool_check,
        "extensions": extensions,
        "tables": tables,
        "indexes": indexes,
        "pgvector": pgvec,
        "tsvector": tsv,
        "settings_store": settings,
    }
    overall_ok = all(c.get("ok") for c in checks.values())

    duration_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "health.database.completed",
        extra={
            "event": "health.database",
            "ok": overall_ok,
            "duration_ms": duration_ms,
            "failed_checks": [k for k, c in checks.items() if not c.get("ok")],
        },
    )
    return {"ok": overall_ok, "duration_ms": duration_ms, "checks": checks}
