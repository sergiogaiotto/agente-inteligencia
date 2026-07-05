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
    Redis cai temporariamente, ou em desenvolvimento single-worker.

    Faz GC periódico de chaves ociosas (CWE-400): sem ele, cada identidade
    distinta (user/IP × bucket) deixava um resíduo permanente no dict, crescendo
    sem limite sob tráfego de muitos IPs.
    """

    _GC_EVERY = 500        # varre a cada N checks
    _GC_HORIZON = 300.0    # remove chaves sem atividade há > 5 min (>> janela de 60s)

    def __init__(self):
        self._buckets: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()
        self._ops = 0

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
            self._ops += 1
            if self._ops >= self._GC_EVERY:
                self._ops = 0
                self._gc(now)
            return count <= limit, max(0, limit - count), reset_in

    def _gc(self, now: float) -> None:
        """Remove chaves cujo timestamp mais recente saiu do horizonte de GC."""
        horizon = now - self._GC_HORIZON
        dead = [k for k, ts in self._buckets.items() if not ts or ts[-1] < horizon]
        for k in dead:
            self._buckets.pop(k, None)


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
    """Retorna key:<hash> (API key), user:<uid> (cookie) ou ip:<addr>.

    Lê o user_id do cookie de sessão ASSINADO (read_session_uid) — um cookie
    forjado não vira uma identidade `user:` (cai no bucket por IP), evitando
    que um atacante escape do rate-limit com cookies arbitrários.

    API key (integração): balde PRÓPRIO por key (F5) — dois frontends atrás do
    MESMO IP não competem pelo mesmo balde. O rate-limit roda ANTES do auth
    (sem api_key_id no state ainda), então a identidade vem do HASH do valor da
    key (sync, sem ida ao banco); a validação real acontece depois no auth. Uma
    key inválida ganha seu próprio balde de 401s BARATOS (sem LLM) — o caminho
    caro (invoke) exige key válida, então o floor de custo permanece protegido.
    """
    from app.core.auth import read_session_uid, _extract_api_key_from_headers

    api_key = _extract_api_key_from_headers(request)
    if api_key:
        import hashlib
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]

    uid = read_session_uid(request)
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
    # Rotas que DISPARAM LLM (caras) → bucket 'workspace' (limite apertado).
    # Allowlist EXPLÍCITA com parênteses: a versão antiga
    # (`... or path.startswith('/agents') and '/run' in path`) sofria de
    # precedência (`and` liga antes de `or`) e deixava /agents/{id}/invoke,
    # /pipelines/{id}/invoke e /wizard/* caírem no bucket genérico (60/min).
    is_llm = (
        path.startswith("/api/v1/workspace")
        or path.startswith("/api/v1/wizard")
        or (path.startswith("/api/v1/agents/")
            and (path.endswith("/invoke") or path.endswith("/run")))
        or (path.startswith("/api/v1/pipelines/") and "/invoke" in path)
    )
    if is_llm:
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
