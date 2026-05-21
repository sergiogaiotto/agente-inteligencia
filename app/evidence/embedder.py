"""Embedder com seletor Azure | Qwen3 (Onda 3 / Onda 4 PR plataforma).

Qwen3 (Onda 4): suporta endpoint OpenAI-compatible custom — reusa URL/key
do OSS source escolhido (oss20b ou oss120b), só muda o path. Permite usar
o mesmo hub interno (ex: Claro hub-gpus) que serve os LLMs também para
embeddings.

Singleton lazy. Graceful: se provider configurado falhar, retorna None —
caller decide degradar (ingest aborta com 503; retriever cai em BM25-only).
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_embedder = None  # singleton


class _EmbedderProtocol(Protocol):
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def aembed_query(self, text: str) -> list[float]: ...


# ───────────────────────────────────────────────────────────────
# Qwen3 — endpoint OpenAI-compatible (POST /embeddings)
# ───────────────────────────────────────────────────────────────


def _qwen3_base_url(oss_url: str, qwen3_path: str) -> str:
    """Constrói o base_url do Qwen3 a partir do path (ou URL absoluta) configurado.

    Dois modos:
    1. **Path relativo** (padrão histórico): reusa scheme://host do OSS_URL e
       concatena com qwen3_path. Útil quando o hub serve Qwen3 no mesmo host
       do OSS.
       Ex: oss_url='https://hub-gpus.claro.com.br/gpt120/v1',
           qwen3_path='embed06b/v1'
         → 'https://hub-gpus.claro.com.br/embed06b/v1'

    2. **URL absoluta**: se qwen3_path já é uma URL completa (começa com http://
       ou https://), usa direto ignorando o oss_url. Cobre o caso em que o
       operador cola a URL do hub inteira no campo (intuitivo, vinha gerando
       URL malformada do tipo https://host/https://...).
       Ex: qwen3_path='https://hub-gpus.claro.com.br/embed06b/v1'
         → 'https://hub-gpus.claro.com.br/embed06b/v1'

    Retorna "" se a entrada é incompleta (sem oss_url e sem URL absoluta).
    """
    qpath = (qwen3_path or "").strip()

    # Modo 2: qwen3_path já é absoluto — usa direto, ignora oss_url.
    parsed_q = urlparse(qpath)
    if parsed_q.scheme in ("http", "https") and parsed_q.netloc:
        return qpath.rstrip("/")

    # Modo 1: precisa do OSS source para reusar scheme://host
    if not oss_url:
        return ""
    parsed = urlparse(oss_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/{qpath.strip('/')}"


class Qwen3Embedder:
    """Embedder que fala /embeddings em endpoint OpenAI-compatible.

    Mantém a interface esperada pelo restante do projeto: `aembed_documents`
    (batch) e `aembed_query` (single).

    `dimensions` (opcional): trunca o vetor de saída no servidor — só funciona
    em modelos Matryoshka como Qwen3-Embedding. Quando None ou 0, o parâmetro
    não vai no payload e o modelo usa sua dim nativa (1024 para 0.6B).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 60,
        dimensions: Optional[int] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "not-needed"
        self.model = model
        self.timeout = timeout
        # Normaliza: 0 e valores inválidos viram None (não envia parâmetro)
        self.dimensions = dimensions if (isinstance(dimensions, int) and dimensions > 0) else None

    def _build_payload(self, input_: Any) -> dict:
        payload: dict = {"model": self.model, "input": input_}
        if self.dimensions:
            payload["dimensions"] = self.dimensions
        return payload

    async def _post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            raise RuntimeError(f"qwen3 HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post(self._build_payload(texts))
        items = data.get("data") or []
        # Ordena por index pra garantir alinhamento com `texts`
        items.sort(key=lambda x: x.get("index", 0))
        return [it["embedding"] for it in items]

    async def aembed_query(self, text: str) -> list[float]:
        data = await self._post(self._build_payload(text))
        items = data.get("data") or []
        if not items:
            raise RuntimeError("qwen3: resposta sem 'data'")
        return items[0]["embedding"]


# ───────────────────────────────────────────────────────────────
# Builders e seletor
# ───────────────────────────────────────────────────────────────


def _build_azure_embedder():
    settings = get_settings()
    if not (settings.azure_openai_api_key and settings.azure_openai_endpoint):
        logger.warning("Azure OpenAI não configurado; embedder Azure indisponível.")
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


def _build_qwen3_embedder():
    settings = get_settings()
    source = (settings.qwen3_source or "oss120b").lower()
    if source == "oss20b":
        oss_url, api_key = settings.oss20b_url, settings.oss20b_api_key
    else:
        oss_url, api_key = settings.oss120b_url, settings.oss120b_api_key
    base_url = _qwen3_base_url(oss_url, settings.qwen3_path)
    if not base_url:
        logger.warning(
            f"Qwen3 selecionado mas OSS source '{source}' não configurado "
            f"(URL vazia ou inválida). Embedder Qwen3 indisponível."
        )
        return None
    return Qwen3Embedder(
        base_url=base_url,
        api_key=api_key or "not-needed",
        model=settings.qwen3_model,
        timeout=settings.llm_timeout_seconds,
        dimensions=int(settings.qwen3_dimensions or 0) or None,
    )


def _build_embedder():
    """Constroi o embedder ativo baseado em settings.embedding_provider."""
    settings = get_settings()
    provider = (settings.embedding_provider or "azure").lower()
    if provider == "qwen3":
        return _build_qwen3_embedder()
    return _build_azure_embedder()


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
