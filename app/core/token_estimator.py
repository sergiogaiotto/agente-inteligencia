"""Estimativa de tokens para texto livre.

Estratégia em camadas:
1. `tiktoken` (preciso, GPT-style BPE) se disponível — lazy import
2. Fallback `len(text) / 4` (heurística para textos em PT/EN — funciona
   bem para a faixa de magnitude que importa pro Diagnóstico do Agente)

A heurística não substitui o cálculo real em produção (o billing usa o
count que o provider retorna no response). Esta função existe só para
DAR UMA ESTIMATIVA para a UI antes da chamada ser feita.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


_TIKTOKEN_AVAILABLE: bool | None = None
_ENCODING_CACHE: dict[str, object] = {}


def _get_encoding(model: str = "gpt-4o"):
    """Lazy import + cache do encoding tiktoken."""
    global _TIKTOKEN_AVAILABLE
    if _TIKTOKEN_AVAILABLE is False:
        return None
    try:
        import tiktoken
        _TIKTOKEN_AVAILABLE = True
    except ImportError:
        _TIKTOKEN_AVAILABLE = False
        logger.info(
            "token_estimator.tiktoken_unavailable",
            extra={
                "event": "token_estimator.fallback",
                "fallback": "len/4 heuristic",
            },
        )
        return None

    cache_key = model.lower()
    if cache_key in _ENCODING_CACHE:
        return _ENCODING_CACHE[cache_key]
    try:
        enc = tiktoken.encoding_for_model(model)
    except Exception:
        # Modelo desconhecido pelo tiktoken (ex.: gpt-oss-120b) — usa cl100k_base
        # (mesma BPE do GPT-4, boa aproximação para a maioria dos modelos).
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            logger.warning(
                "token_estimator.encoding_fetch_failed",
                extra={
                    "event": "token_estimator.fallback",
                    "model": model,
                    "error_type": type(e).__name__,
                    "error_msg": str(e)[:200],
                },
            )
            return None
    _ENCODING_CACHE[cache_key] = enc
    return enc


def estimate_tokens(text: str, model: str = "gpt-4o") -> int:
    """Estima quantidade de tokens em `text`.

    Args:
        text: conteúdo a estimar (vazio → 0)
        model: nome do modelo para escolher o encoding tiktoken adequado.

    Returns:
        Inteiro >= 0. Heurística char/4 quando tiktoken indisponível.
    """
    if not text:
        return 0
    enc = _get_encoding(model)
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception:
            pass
    # Heurística: 4 chars ≈ 1 token para textos em latim
    return max(1, len(text) // 4)
