"""Embedder Azure OpenAI (Onda 3).

Usa AzureOpenAIEmbeddings do langchain-openai. Singleton lazy. Graceful:
se Azure não configurado ou a chamada falhar, retorna None — caller decide
como degradar (ingest aborta com 503; retriever cai em BM25-only).

Não suporta OpenAI público (sem Azure) por simplicidade — basta wirar
quando tivermos demanda real.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_embedder = None  # singleton


def _build_embedder():
    """Constroi o embedder. Onda 4b: prefere o gateway LiteLLM quando ativo.

    - Gateway mode: usa OpenAIEmbeddings apontando para LiteLLM com model
      'azure/text-embedding-3-small' (definido no config.yaml). Gateway
      cuida de chave/endpoint/api_version reais.
    - Direct mode (default): AzureOpenAIEmbeddings direto, comportamento original.
    """
    settings = get_settings()

    if settings.llm_gateway_enabled and settings.llm_gateway_master_key:
        try:
            from langchain_openai import OpenAIEmbeddings
            return OpenAIEmbeddings(
                model=f"azure/{settings.azure_openai_embeddings_deployment}",
                api_key=settings.llm_gateway_master_key,
                base_url=f"{settings.llm_gateway_url.rstrip('/')}/v1",
                # check_embedding_ctx_length=False: gateway cuida do truncate.
                check_embedding_ctx_length=False,
            )
        except Exception as e:
            logger.warning(f"Falha ao instanciar embedder via gateway: {e}; tentando direto")
            # Cai no path direto abaixo

    if not (settings.azure_openai_api_key and settings.azure_openai_endpoint):
        logger.warning("Azure OpenAI não configurado; embedder retornará None.")
        return None
    try:
        from langchain_openai import AzureOpenAIEmbeddings
        return AzureOpenAIEmbeddings(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
            azure_deployment=settings.azure_openai_embeddings_deployment,
        )
    except Exception as e:
        logger.warning(f"Falha ao instanciar AzureOpenAIEmbeddings: {e}")
        return None


def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder()
    return _embedder


async def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Gera embeddings em batch. Retorna None se embedder indisponível.

    Em caso de erro de API (rate-limit, timeout), retorna None depois de logar —
    caller (ingest) reporta 503 e usuário re-tenta.
    """
    if not texts:
        return []
    emb = _get_embedder()
    if emb is None:
        return None
    try:
        # langchain_openai usa httpx async por baixo; aembed_documents é o método async oficial.
        return await emb.aembed_documents(texts)
    except Exception as e:
        logger.warning(f"embed_texts falhou: {type(e).__name__}: {e}")
        return None


async def embed_query(text: str) -> Optional[list[float]]:
    """Embedding de uma query (single). Wraps aembed_query."""
    emb = _get_embedder()
    if emb is None:
        return None
    try:
        return await emb.aembed_query(text)
    except Exception as e:
        logger.warning(f"embed_query falhou: {type(e).__name__}: {e}")
        return None
