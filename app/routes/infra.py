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
