"""Rate-limit por usuário/IP — sliding window via Redis (fallback memory).

Defesa contra **OWASP LLM04 (Model DoS)** e brute-force em /login.

Algoritmo: sliding window com ZSET no Redis.
  ZADD   key score=now_ms  member=now_ms
  ZREMRANGEBYSCORE key 0 (now_ms - window_ms)
  ZCARD  key               → quantidade na janela
  EXPIRE key window+5      → autolimpa entradas órfãs
  pipeline atômico

Fallback memory: se Redis estiver indisponível, usa dict in-process. Não
serve para múltiplos workers (cada worker tem seu próprio bucket), mas
mantém o serviço funcionando — preferível a 502 enquanto o Redis volta.

Identidade: cookie `user_id` quando autenticado; senão IP do cliente
(considerando `X-Forwarded-For` quando atrás de Caddy/Traefik). Chaves
têm escopo por path-prefix para que rotas pesadas (workspace) tenham
limite independente das leves (dashboard).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class _LimiterImpl(Protocol):
    async def check(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        """Retorna (allowed, remaining, reset_in_seconds)."""
        ...


class _RedisLimiter:
    def __init__(self, client):
        self.client = client

    async def check(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - window * 1000
        pipe = self.client.pipeline()
        pipe.zremrangebyscore(key, 0, cutoff)
        pipe.zadd(key, {str(now_ms): now_ms})
        pipe.zcard(key)
        pipe.expire(key, window + 5)
        results = await pipe.execute()
        count = int(results[2])
        allowed = count <= limit
        remaining = max(0, limit - count)
        if not allowed:
            oldest = await self.client.zrange(key, 0, 0, withscores=True)
            if oldest:
                # libera quando o membro mais antigo sai da janela
                _, oldest_score = oldest[0]
                reset_in = max(1, int((float(oldest_score) - cutoff) / 1000))
            else:
                reset_in = window
        else:
            reset_in = window
        return allowed, remaining, reset_in


class _MemoryLimiter:
    """Fallback in-process. Não compartilha entre workers — aceitável quando
    Redis cai temporariamente, ou em desenvolvimento single-worker."""

    def __init__(self):
        self._buckets: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        async with self._lock:
            now = time.time()
            cutoff = now - window
            bucket = self._buckets.setdefault(key, [])
            bucket[:] = [t for t in bucket if t > cutoff]
            bucket.append(now)
            count = len(bucket)
            if count > limit:
                reset_in = max(1, int(bucket[0] + window - now))
            else:
                reset_in = window
            return count <= limit, max(0, limit - count), reset_in


class RateLimiter:
    """Singleton — escolhe Redis ou memory na primeira chamada."""

    def __init__(self):
        self._impl: Optional[_LimiterImpl] = None
        self._init_lock = asyncio.Lock()

    async def _get_impl(self) -> _LimiterImpl:
        if self._impl is not None:
            return self._impl
        async with self._init_lock:
            if self._impl is not None:
                return self._impl
            try:
                import redis.asyncio as aioredis
                client = aioredis.from_url(
                    get_settings().redis_url,
                    decode_responses=True,
                    socket_connect_timeout=2,
                )
                await asyncio.wait_for(client.ping(), timeout=2)
                self._impl = _RedisLimiter(client)
                logger.info("RateLimiter: usando Redis backend.")
            except Exception as e:
                logger.warning(f"RateLimiter: Redis indisponível ({e}) — fallback memory.")
                self._impl = _MemoryLimiter()
            return self._impl

    async def check(self, key: str, limit: int, window: int) -> tuple[bool, int, int]:
        impl = await self._get_impl()
        return await impl.check(key, limit, window)


limiter = RateLimiter()


# ═══════════════════════════════════════════════════════════════
# Identidade do cliente — cookie ou IP (X-Forwarded-For-aware)
# ═══════════════════════════════════════════════════════════════


def _client_identity(request: Request) -> str:
    """Retorna user:<uid> se autenticado, ip:<addr> caso contrário."""
    uid = request.cookies.get("user_id")
    if uid:
        return f"user:{uid}"
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # primeiro IP da cadeia é o cliente real
        ip = xff.split(",")[0].strip()
        if ip:
            return f"ip:{ip}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


# ═══════════════════════════════════════════════════════════════
# Limites por path — buckets separados para evitar starvation
# ═══════════════════════════════════════════════════════════════


def _bucket_for_path(path: str) -> tuple[str, int]:
    """Retorna (bucket_name, limit_per_window) para uma rota."""
    settings = get_settings()
    if path.startswith("/api/v1/workspace") or path.startswith("/api/v1/agents") and "/run" in path:
        return ("workspace", settings.rate_limit_workspace_per_min)
    if path.startswith("/api/v1/users/login") or path.endswith("/login"):
        return ("auth", settings.rate_limit_auth_per_min)
    if path.startswith("/api/"):
        return ("api", settings.rate_limit_default_per_min)
    return ("static", 0)  # 0 = isento


# ═══════════════════════════════════════════════════════════════
# Middleware FastAPI/Starlette
# ═══════════════════════════════════════════════════════════════


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return await call_next(request)

        path = request.url.path
        bucket, limit = _bucket_for_path(path)
        if limit <= 0:
            return await call_next(request)
        # /api/health não conta — usado por Docker healthcheck
        if path == "/api/health":
            return await call_next(request)

        identity = _client_identity(request)
        key = f"rl:{bucket}:{identity}"
        allowed, remaining, reset_in = await limiter.check(
            key, limit, settings.rate_limit_window_seconds
        )

        if not allowed:
            logger.warning(f"rate-limit hit: bucket={bucket} identity={identity} limit={limit}")
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "bucket": bucket,
                    "retry_after": reset_in,
                    "message": (
                        f"Você atingiu o limite de {limit} requisições/{settings.rate_limit_window_seconds}s "
                        f"para este tipo de operação. Tente novamente em {reset_in}s."
                    ),
                },
                headers={
                    "Retry-After": str(reset_in),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_in),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
