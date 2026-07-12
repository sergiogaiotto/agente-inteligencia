"""Circuit-breaker por-provider do egress LLM — cross-worker via Redis
(fallback in-process). Espelha o padrão do ``RateLimiter`` (app/core/ratelimit.py).

PROBLEMA (Onda 2, 33.1.0): quando um provider LLM cai, cada invoke/juiz/wizard
paga o timeout de conexão ANTES de cair no fallback — httpx trava até
``llm_timeout_seconds`` (300s no gpt-oss), Maritaca 120s, Ollama 180s, ou o
SYN-timeout do SO (~60-127s) num host que dropa pacotes. Sob o pool asyncpg 5/20
(#556), poucas interações presas nesse timeout exaurem o pool e derrubam a
plataforma inteira. As métricas Prometheus do #565 REVELAM o raio; este breaker
o CONTÉM: depois de N falhas de ALCANCE num provider, o circuito ABRE e as
chamadas seguintes são curto-circuitadas (não pagam o timeout) por um cooldown;
passado o cooldown, um número limitado de SONDAS (half-open) testa a recuperação —
sucesso FECHA, falha REABRE.

RELAÇÃO COM O ENGINE: ``app/agents/engine.py`` já tinha um marcador de "provider
caído" por-processo (``_llm_down_at``, TTL 90s, threshold-1, "reordena-não-pula",
fix do incidente Aurora 2026-07-02). Este módulo é a GENERALIZAÇÃO cross-worker
dele — o engine passa a alimentá-lo e consultá-lo, e os wrappers que NÃO
reordenam (``generate_with_hosted_fallback`` do juiz/verifier e
``_wizard_llm_complete``) ganham skip real (pulam o provider aberto).

SEMÂNTICA CROSS-WORKER: com Redis, o estado é COMPARTILHADO — um circuito aberto
vale para todo o deploy e a contagem de falhas é agregada globalmente (a frota
inteira para de bater no provider morto após N falhas TOTAIS). O fallback
in-process é POR-WORKER (cada processo tem seu estado; contagens não somam;
restart zera) — aceitável quando o Redis cai: fail-open, preferível a derrubar o
serviço, exatamente como o ``_MemoryLimiter`` documenta.

ABRE SÓ em falha de ALCANCE (rede/timeout/URL ausente). O CALLER decide isso com
o detector canônico ``is_llm_unreachable`` (app/core/llm_providers) e só então
chama ``record_failure``. Erros de PARÂMETRO/CONTEÚDO (400) ou 4xx/5xx com o
provider VIVO NÃO devem chamar ``record_failure`` — o provider respondeu, não
está morto.

CHAVE = nome CANÔNICO do provider (o caller passa ``canonical_provider(name)``;
openai≡azure). Este módulo trata a string como opaca e NÃO importa
``llm_providers`` (evita import circular, já que ``llm_providers`` importa este).

KEYSPACE PEQUENO: as chaves são providers (azure, gpt-oss-20b/120b, maritaca,
ollama, openai_public — ~6), diferente do rate-limit (user/IP, ilimitado). Por
isso NÃO há GC: o dict in-process nunca cresce além de um punhado de entradas.

INVARIANTE DE DESEMPENHO: toda operação do breaker é fail-open — qualquer erro
do próprio breaker (Redis piscou no meio) é engolido e degrada para "deixa
passar", NUNCA propaga para o caminho quente do invoke.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Protocol

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Estados do circuito.
_CLOSED = "closed"
_OPEN = "open"
_HALF_OPEN = "half_open"


def _now() -> float:
    """Relógio monotônico (indireção para os testes poderem controlá-lo)."""
    return time.monotonic()


def _emit_open(provider: str) -> None:
    """Log estruturado + métrica na TRANSIÇÃO para aberto (não a cada falha)."""
    logger.warning(
        "llm.breaker.open",
        extra={"event": "llm.breaker.open", "provider": provider},
    )
    try:
        from app.core import metrics
        metrics.record_breaker_open(provider)
    except Exception:  # métrica é best-effort — nunca quebra o caminho quente
        pass


def note_short_circuit(provider: str, purpose: str) -> None:
    """Registra que uma chamada foi PULADA por circuito aberto (timeout evitado).

    Chamado pelos wrappers de resiliência (``generate_with_hosted_fallback``,
    ``_wizard_llm_complete``) no ponto de skip — mede exatamente o ganho do
    breaker (quantos timeouts de provider morto deixaram de ser pagos)."""
    logger.info(
        "llm.breaker.skip",
        extra={"event": "llm.breaker.skip", "provider": provider, "purpose": purpose},
    )
    try:
        from app.core import metrics
        metrics.record_breaker_short_circuit(provider)
    except Exception:
        pass


class _BreakerBackend(Protocol):
    async def is_open(self, provider: str) -> bool:
        """Peek SEM efeito colateral: True sse o circuito está OPEN agora."""
        ...

    async def allow(self, provider: str) -> bool:
        """Gate de aquisição: True se a chamada pode prosseguir (consome uma
        sonda em half-open). False = curto-circuitar (OPEN, ou sondas esgotadas)."""
        ...

    async def record_success(self, provider: str) -> None: ...

    async def record_failure(self, provider: str) -> None: ...


class _MemoryBreaker:
    """Fallback in-process. Por-worker — não compartilha entre processos.
    Aceitável quando o Redis cai: fail-open, mantém o serviço de pé."""

    def __init__(self):
        # provider -> {"failures": int, "opened_at": float|None, "probes": int}
        self._circuits: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _state(c: dict, now: float, cooldown: int) -> str:
        if c["opened_at"] is None:
            return _CLOSED
        if now - c["opened_at"] < cooldown:
            return _OPEN
        return _HALF_OPEN

    async def is_open(self, provider: str) -> bool:
        s = get_settings()
        async with self._lock:
            c = self._circuits.get(provider)
            if not c:
                return False
            return self._state(c, _now(), s.cb_cooldown_seconds) == _OPEN

    async def allow(self, provider: str) -> bool:
        s = get_settings()
        async with self._lock:
            c = self._circuits.get(provider)
            if not c:
                return True
            st = self._state(c, _now(), s.cb_cooldown_seconds)
            if st == _CLOSED:
                return True
            if st == _OPEN:
                return False
            # HALF_OPEN — concede até max_probes sondas concorrentes; excedentes
            # são bloqueadas (evita thundering-herd sobre um provider que se
            # recupera).
            if c["probes"] < max(1, s.cb_half_open_max_probes):
                c["probes"] += 1
                return True
            return False

    async def record_success(self, provider: str) -> None:
        async with self._lock:
            c = self._circuits.get(provider)
            if c is not None:
                c["failures"] = 0
                c["opened_at"] = None
                c["probes"] = 0

    async def record_failure(self, provider: str) -> None:
        s = get_settings()
        threshold = max(1, s.cb_failure_threshold)
        async with self._lock:
            now = _now()
            c = self._circuits.setdefault(
                provider, {"failures": 0, "opened_at": None, "probes": 0}
            )
            st = self._state(c, now, s.cb_cooldown_seconds)
            if st == _HALF_OPEN:
                # sonda falhou → reabre imediatamente.
                c["opened_at"] = now
                c["probes"] = 0
                _emit_open(provider)
                return
            if st == _OPEN:
                # já aberto (o engine pode tentar um provider aberto quando é o
                # único candidato) — só mantém; não reemite.
                return
            c["failures"] += 1
            if c["failures"] >= threshold:
                c["opened_at"] = now
                c["probes"] = 0
                _emit_open(provider)


class _RedisBreaker:
    """Backend Redis — estado COMPARTILHADO entre workers/réplicas.

    Chaves por provider ``p``:
      ``cb:fail:{p}``  contador de falhas (INCR; TTL = cooldown*3, decai sozinho)
      ``cb:open:{p}``  presença = OPEN; TTL = cooldown (auto half-open ao expirar)
      ``cb:probe:{p}`` sondas concedidas em half-open (INCR; TTL = cooldown)

    Ops individuais são atômicas (INCR/SET NX/DEL). O compound de ``allow``
    (EXISTS→GET→INCR) tem uma janela de corrida minúscula e BENIGNA (talvez uma
    sonda a mais). Sem Lua para manter simples e auditável."""

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _k(provider: str, kind: str) -> str:
        return f"cb:{kind}:{provider}"

    async def is_open(self, provider: str) -> bool:
        return bool(await self.client.exists(self._k(provider, "open")))

    async def allow(self, provider: str) -> bool:
        s = get_settings()
        if await self.client.exists(self._k(provider, "open")):
            return False
        # OPEN expirou (ou nunca abriu). HALF_OPEN sse o contador de falhas ainda
        # atingiu o threshold; senão CLOSED.
        fails = await self.client.get(self._k(provider, "fail"))
        if fails is None or int(fails) < max(1, s.cb_failure_threshold):
            return True  # CLOSED
        # HALF_OPEN — INCR atômico concede até max_probes.
        probe_key = self._k(provider, "probe")
        n = await self.client.incr(probe_key)
        if n == 1:
            await self.client.expire(probe_key, max(1, s.cb_cooldown_seconds))
        return n <= max(1, s.cb_half_open_max_probes)

    async def record_success(self, provider: str) -> None:
        await self.client.delete(
            self._k(provider, "fail"),
            self._k(provider, "open"),
            self._k(provider, "probe"),
        )

    async def record_failure(self, provider: str) -> None:
        s = get_settings()
        cooldown = max(1, s.cb_cooldown_seconds)
        threshold = max(1, s.cb_failure_threshold)
        open_key = self._k(provider, "open")
        # Estava aberto? (falha durante OPEN não deve reemitir/reabrir.)
        already_open = bool(await self.client.exists(open_key))
        fail_key = self._k(provider, "fail")
        n = await self.client.incr(fail_key)
        await self.client.expire(fail_key, cooldown * 3)
        if not already_open and n >= threshold:
            # Abre (ou reabre a partir de half-open). SET NX detecta a TRANSIÇÃO
            # para emitir a métrica uma vez só sob concorrência.
            was_set = await self.client.set(open_key, "1", ex=cooldown, nx=True)
            await self.client.delete(self._k(provider, "probe"))
            if was_set:
                _emit_open(provider)


class CircuitBreaker:
    """Singleton — escolhe Redis (cross-worker) ou memory (por-worker) na 1ª
    chamada, idêntico ao ``RateLimiter``. Todo método é fail-open: erro do
    backend degrada para 'deixa passar' e NUNCA propaga ao caminho quente."""

    def __init__(self):
        self._impl: Optional[_BreakerBackend] = None
        self._init_lock = asyncio.Lock()

    async def _get_impl(self) -> _BreakerBackend:
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
                self._impl = _RedisBreaker(client)
                logger.info("CircuitBreaker: usando Redis backend (cross-worker).")
            except Exception as e:
                logger.warning(
                    f"CircuitBreaker: Redis indisponível ({e}) — fallback memory (por-worker)."
                )
                self._impl = _MemoryBreaker()
            return self._impl

    async def is_open(self, provider: str) -> bool:
        if not get_settings().circuit_breaker_enabled:
            return False
        try:
            impl = await self._get_impl()
            return await impl.is_open(provider)
        except Exception as e:
            logger.warning(f"CircuitBreaker.is_open erro ({e}) — fail-open")
            return False

    async def allow(self, provider: str) -> bool:
        if not get_settings().circuit_breaker_enabled:
            return True
        try:
            impl = await self._get_impl()
            return await impl.allow(provider)
        except Exception as e:
            logger.warning(f"CircuitBreaker.allow erro ({e}) — fail-open")
            return True

    async def record_success(self, provider: str) -> None:
        if not get_settings().circuit_breaker_enabled:
            return
        try:
            impl = await self._get_impl()
            await impl.record_success(provider)
        except Exception as e:
            logger.warning(f"CircuitBreaker.record_success erro ({e})")

    async def record_failure(self, provider: str) -> None:
        if not get_settings().circuit_breaker_enabled:
            return
        try:
            impl = await self._get_impl()
            await impl.record_failure(provider)
        except Exception as e:
            logger.warning(f"CircuitBreaker.record_failure erro ({e})")


breaker = CircuitBreaker()
