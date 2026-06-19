"""Saúde dos modelos (chat/roteamento + embeddings) — probe de inferência leve.

Resolve o mapa de roteamento de LLM (task_type → provider/model, via
``app.llm_routing``) e o provider de embeddings (``app.evidence.embedder``), e
sonda cada modelo DISTINTO com uma chamada MÍNIMA (completa 1 token / embeda um
texto curto). Resultado é cacheado (TTL) e as sondas rodam em PARALELO com
timeout curto — assim um endpoint inacessível (ex.: hub fora da rede) não trava
a resposta.

Serve ao endpoint ``GET /api/v1/llm/health``, que alimenta o chip no header:
informa o que será usado dali em diante e alerta quando algum modelo está
indisponível ou quando um fallback (ex.: embeddings qwen3→azure) está ativo.

Custo: 1 token por modelo distinto de chat + 1 embed, no máximo a cada
``_CACHE_TTL_S``. Modelos repetidos no roteamento são deduplicados.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Timeout por sonda — curto para falhar rápido em endpoint inacessível.
_PROBE_TIMEOUT_S = 8
# Cache do resultado — evita sondar a cada page-load/header render.
_CACHE_TTL_S = 300

_cache: Optional[dict] = None
_cache_at: float = 0.0

# Papéis de roteamento reportados (TASK_TYPES + o fallback multimodal).
_EXTRA_ROLES = ("multimodal_fallback",)


async def _probe_chat(provider_name: str, model: str) -> dict:
    """Sonda mínima de um modelo de chat: completa 1 token. Nunca levanta."""
    from app.core.llm_providers import get_provider

    t = time.time()
    try:
        prov = get_provider(provider_name, model=model)
        res = await asyncio.wait_for(
            prov.generate([{"role": "user", "content": "ping"}], max_tokens=1),
            timeout=_PROBE_TIMEOUT_S,
        )
        ok = bool(res) and res.get("content") is not None
        return {"ok": ok, "latency_ms": int((time.time() - t) * 1000),
                "error": None if ok else "resposta vazia"}
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": int((time.time() - t) * 1000),
                "error": f"timeout (>{_PROBE_TIMEOUT_S}s)"}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t) * 1000),
                "error": f"{type(e).__name__}: {str(e)[:120]}"}


async def _probe_embedding() -> dict:
    """Sonda de embeddings: testa CADA provider da cadeia com timeout CURTO.

    Não usa resolve_effective_provider() porque ele tenta o provider primário
    com o timeout longo do LLM (ex.: qwen3 pendura ~60s antes do fallback) — aqui
    cada provider tem o mesmo timeout curto das sondas de chat, então o resultado
    reflete o fallback rapidamente. Ao achar um provider que responde, AQUECE o
    cache do embedder (``_embedder``/``_effective_provider``) — assim a primeira
    ingestão real já sai pelo provider efetivo, sem repetir o hang do primário.
    Nunca levanta.
    """
    from app.evidence import embedder as emb

    t = time.time()
    chain = emb._embedding_chain()
    configured = chain[0] if chain else "azure"
    last_err: Optional[Exception] = None
    for prov in chain:
        builder = emb._BUILDERS.get(prov)
        if builder is None:
            continue
        inst = builder()
        if inst is None:
            continue  # provider não configurado (ex.: Azure sem key)
        try:
            await asyncio.wait_for(inst.aembed_query("ping"), timeout=_PROBE_TIMEOUT_S)
        except Exception as ex:
            last_err = ex
            continue
        # Sucesso — aquece o cache do embedder com o provider efetivo.
        emb._embedder = inst
        emb._effective_provider = prov
        return {
            "ok": True,
            "configured": configured,
            "effective": prov,
            "fallback_active": prov != configured,
            "dim": emb.get_active_embedding_dim(),
            "latency_ms": int((time.time() - t) * 1000),
            "error": None,
        }
    return {
        "ok": False,
        "configured": configured,
        "effective": None,
        "fallback_active": False,
        "dim": None,
        "latency_ms": int((time.time() - t) * 1000),
        "error": (f"{type(last_err).__name__}" if last_err else "nenhum provider configurado"),
    }


async def get_model_health(force: bool = False) -> dict:
    """Mapa de saúde dos modelos em uso, com sondas de inferência (cacheado).

    Retorna::

        {
          "all_ok": bool,            # todos os modelos (chat + embeddings) ok?
          "any_fallback": bool,      # algum fallback ativo (ex.: embeddings)?
          "chat": {                  # por papel de roteamento
            "tool_calling": {"provider","model","ok","error","latency_ms"}, ...
          },
          "embeddings": {"configured","effective","fallback_active","dim","ok",...},
        }
    """
    global _cache, _cache_at
    now = time.time()
    if not force and _cache is not None and (now - _cache_at) < _CACHE_TTL_S:
        return _cache

    from app.llm_routing import DEFAULT_ROUTING, TASK_TYPES, _split_pm, load_routing

    routing = await load_routing()

    roles: dict[str, dict] = {}
    distinct: dict[tuple[str, str], None] = {}
    for role in list(TASK_TYPES) + list(_EXTRA_ROLES):
        spec = routing.get(role) or DEFAULT_ROUTING.get(role, "")
        if not spec:
            continue
        provider, model = _split_pm(spec)
        roles[role] = {"provider": provider, "model": model}
        distinct[(provider, model)] = None

    # Sonda os modelos DISTINTOS em paralelo (dedup evita sondar gpt-oss 2x).
    keys = list(distinct.keys())
    chat_results = await asyncio.gather(*[_probe_chat(p, m) for (p, m) in keys])
    probe_map = {keys[i]: chat_results[i] for i in range(len(keys))}
    for info in roles.values():
        pr = probe_map.get((info["provider"], info["model"]), {})
        info["ok"] = pr.get("ok", False)
        info["error"] = pr.get("error")
        info["latency_ms"] = pr.get("latency_ms")

    emb = await _probe_embedding()

    all_ok = all(r.get("ok") for r in roles.values()) and bool(emb.get("ok"))
    result = {
        "all_ok": all_ok,
        "any_fallback": bool(emb.get("fallback_active")),
        "chat": roles,
        "embeddings": emb,
    }
    _cache = result
    _cache_at = now
    return result


def invalidate_cache() -> None:
    """Força nova sondagem no próximo get_model_health (ex.: após mudar settings)."""
    global _cache_at
    _cache_at = 0.0
