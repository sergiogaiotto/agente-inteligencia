"""Status da infraestrutura — checa todos os serviços do compose em paralelo.

Endpoint único `/api/v1/infra/status` retorna lista de services com:
- ok: bool (responde no health check)
- latency_ms: float
- error: str opcional (se ok=False)
- url: link pra UI nativa quando existe (Qdrant dashboard, Grafana, etc.)
- hint: dica contextual (ex: "rode com --profile full")

Frontend renderiza isso como cards.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/infra", tags=["infra"])


# Timeout curto: cada check tem que ser rápido — o usuário está olhando a página.
_TIMEOUT = 1.5


async def _check_http(
    name: str,
    health_url: str,
    *,
    description: str,
    ui_url: Optional[str] = None,
    expect_status: tuple = (200,),
    profile_full: bool = False,
) -> dict:
    """Checa um serviço HTTP com httpx GET. Retorna dict pro frontend."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(health_url)
            ok = r.status_code in expect_status
            return {
                "name": name,
                "ok": ok,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "description": description,
                "ui_url": ui_url,
                "health_url": health_url,
                "status_code": r.status_code,
                "error": None if ok else f"HTTP {r.status_code}",
                "hint": None if ok else (
                    "Serviço opcional — rode `docker compose --profile full up -d`"
                    if profile_full else None
                ),
            }
    except Exception as e:
        return {
            "name": name,
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": description,
            "ui_url": ui_url,
            "health_url": health_url,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": (
                "Serviço opcional — rode `docker compose --profile full up -d`"
                if profile_full else None
            ),
        }


async def _check_postgres() -> dict:
    """Postgres não tem HTTP — usa o pool asyncpg do app pra um SELECT 1."""
    from app.core.database import _get_pool
    t0 = time.perf_counter()
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
        return {
            "name": "postgres",
            "ok": True,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Banco principal (agentes, interações, evidências)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None,
            "hint": None,
        }
    except Exception as e:
        return {
            "name": "postgres",
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Banco principal (agentes, interações, evidências)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": None,
        }


async def _check_redis() -> dict:
    """Redis ping via redis.asyncio (mesmo client usado em ratelimit.py)."""
    t0 = time.perf_counter()
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = aioredis.from_url(url, socket_timeout=_TIMEOUT)
        try:
            pong = await client.ping()
        finally:
            await client.close()
        ok = bool(pong)
        return {
            "name": "redis",
            "ok": ok,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Cache de contexto + rate-limit (Onda 1)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": None if ok else "PING não respondeu PONG",
            "hint": None,
        }
    except Exception as e:
        return {
            "name": "redis",
            "ok": False,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "description": "Cache de contexto + rate-limit (Onda 1)",
            "ui_url": None,
            "health_url": None,
            "status_code": None,
            "error": f"{type(e).__name__}: {str(e)[:80]}",
            "hint": None,
        }


async def _qdrant_details() -> dict:
    """Lista coleções Qdrant com points_count + dimensão dos vetores."""
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{qdrant_url}/collections")
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}", "collections": []}
            cols = (r.json().get("result") or {}).get("collections") or []
            # Para cada coleção, busca detalhes em paralelo
            async def _one(name: str) -> dict:
                try:
                    rr = await client.get(f"{qdrant_url}/collections/{name}")
                    if rr.status_code != 200:
                        return {"name": name, "error": f"HTTP {rr.status_code}"}
                    res = (rr.json().get("result") or {})
                    vec = ((res.get("config") or {}).get("params") or {}).get("vectors") or {}
                    # Qdrant pode retornar `vectors` como dict simples OU como dict de named vectors.
                    # Para named vectors, pega o primeiro size disponível.
                    size = vec.get("size")
                    if size is None and isinstance(vec, dict):
                        for v in vec.values():
                            if isinstance(v, dict) and "size" in v:
                                size = v["size"]
                                break
                    return {
                        "name": name,
                        "points_count": res.get("points_count", 0),
                        "indexed_vectors_count": res.get("indexed_vectors_count", 0),
                        "segments_count": res.get("segments_count", 0),
                        "vector_size": size,
                        "status": res.get("status", "unknown"),
                    }
                except Exception as e:
                    return {"name": name, "error": f"{type(e).__name__}: {str(e)[:60]}"}

            details = await asyncio.gather(*[_one(c["name"]) for c in cols])
            return {"ok": True, "collections": details}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}", "collections": []}


async def _redis_details() -> dict:
    """Estatísticas do Redis via INFO."""
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
        client = aioredis.from_url(url, socket_timeout=_TIMEOUT, decode_responses=True)
        try:
            info = await client.info()
            # Hit rate: hits / (hits + misses). Útil pra avaliar eficiência do cache.
            hits = int(info.get("keyspace_hits", 0))
            misses = int(info.get("keyspace_misses", 0))
            total = hits + misses
            hit_rate = round(hits / total * 100, 1) if total > 0 else None
            # Keys count: o INFO retorna db0 como string "keys=N,expires=N,avg_ttl=N"
            db0 = info.get("db0", {})
            if isinstance(db0, dict):
                keys = int(db0.get("keys", 0))
            else:
                # Fallback: parsing string
                keys = 0
                for part in str(db0).split(","):
                    if part.startswith("keys="):
                        keys = int(part.split("=")[1])
                        break
        finally:
            await client.close()
        return {
            "ok": True,
            "used_memory_human": info.get("used_memory_human", "?"),
            "connected_clients": info.get("connected_clients", 0),
            "total_commands_processed": info.get("total_commands_processed", 0),
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_rate_pct": hit_rate,
            "keys_db0": keys,
            "redis_version": info.get("redis_version", "?"),
            "uptime_in_days": info.get("uptime_in_days", 0),
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


async def _postgres_details() -> dict:
    """Contagens das tabelas principais (agentes, interações, evidências, etc.).

    Reusa o pool asyncpg do app — query única com UNION ALL pra performance.
    """
    from app.core.database import _get_pool
    try:
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch("""
                SELECT 'agents' AS table_name, COUNT(*)::bigint AS count FROM agents
                UNION ALL SELECT 'skills', COUNT(*)::bigint FROM skills
                UNION ALL SELECT 'interactions', COUNT(*)::bigint FROM interactions
                UNION ALL SELECT 'turns', COUNT(*)::bigint FROM turns
                UNION ALL SELECT 'knowledge_sources', COUNT(*)::bigint FROM knowledge_sources
                UNION ALL SELECT 'api_connectors', COUNT(*)::bigint FROM api_connectors
                UNION ALL SELECT 'audit_log', COUNT(*)::bigint FROM audit_log
            """)
            return {"ok": True, "counts": {r["table_name"]: r["count"] for r in rows}}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


@router.get("/details")
async def infra_details():
    """Métricas detalhadas dos serviços de dados — Qdrant collections,
    Redis INFO e contagens das tabelas Postgres principais.

    Diferente de /status (binário ok/error), /details traz contadores e
    configuração que mudam ao longo do uso.
    """
    qdrant, redis, pg = await asyncio.gather(
        _qdrant_details(),
        _redis_details(),
        _postgres_details(),
    )
    return {"qdrant": qdrant, "redis": redis, "postgres": pg}


@router.get("/status")
async def infra_status():
    """Status agregado de todos os serviços do compose.

    Checa em paralelo (asyncio.gather) — total ~1.5s no pior caso (timeout).
    """
    qdrant_url = os.environ.get("QDRANT_URL", "http://qdrant:6333")
    opa_url = os.environ.get("OPA_URL", "http://opa:8181")

    # UIs nativas — quando o app está rodando localmente, esses links abrem no browser.
    # Em produção atrás do Caddy, esses ports não são expostos publicamente, então
    # os links só funcionam de quem tem SSH tunnel ou acesso à rede interna.
    ui_qdrant = "http://localhost:6333/dashboard"
    ui_grafana = "http://localhost:3000"

    checks = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_http(
            "qdrant",
            f"{qdrant_url}/healthz",
            description="Vector DB para RAG (Onda 3)",
            ui_url=ui_qdrant,
        ),
        _check_http(
            "opa",
            f"{opa_url}/health",
            description="Policy as Code — autorização (Onda 4a)",
        ),
        # Profile full: opcionais, podem não estar subidos.
        _check_http(
            "tempo",
            "http://tempo:3200/ready",
            description="Backend de traces OTLP (Onda 2)",
            ui_url=ui_grafana,
            profile_full=True,
        ),
        _check_http(
            "loki",
            "http://loki:3100/ready",
            description="Backend de logs estruturados (Onda 2)",
            ui_url=ui_grafana,
            profile_full=True,
        ),
        _check_http(
            "grafana",
            "http://grafana:3000/api/health",
            description="UI de traces, logs e métricas",
            ui_url=ui_grafana,
            profile_full=True,
        ),
        _check_http(
            "promtail",
            # promtail expõe /metrics e /ready em :9080
            "http://promtail:9080/ready",
            description="Coletor de logs do docker → Loki",
            profile_full=True,
        ),
    )

    return {
        "services": checks,
        "summary": {
            "total": len(checks),
            "healthy": sum(1 for c in checks if c["ok"]),
            "unhealthy": sum(1 for c in checks if not c["ok"]),
        },
    }
