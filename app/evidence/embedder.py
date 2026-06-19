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

_embedder = None            # embedder EFETIVO em cache (o provider que respondeu)
_effective_provider = None  # nome do provider efetivo ('qwen3'|'azure')


def _provider_dim(provider: str) -> int:
    """Dimensão do embedding para um provider. Qwen3 com `qwen3_dimensions=0`
    cai no default do modelo (1024, Qwen3-Embedding-0.6B); Azure
    (text-embedding-3-small) é fixo em 1536."""
    if (provider or "").lower() == "qwen3":
        configured = int(getattr(get_settings(), "qwen3_dimensions", 0) or 0)
        return configured or 1024
    return 1536  # Azure (e qualquer provider desconhecido) cai aqui.


def get_active_embedding_dim() -> int:
    """Retorna a dimensão do embedder ATIVO.

    Segue o provider EFETIVO (`_effective_provider`) quando já resolvido — ou
    seja, o que de fato respondeu, possivelmente via fallback de roteamento
    (ex.: qwen3 inacessível → azure). Sem isso a coluna pgvector seria criada
    com a dim do provider CONFIGURADO (qwen3=1024) enquanto os vetores reais
    viriam do fallback (azure=1536), causando drift no upsert. Antes de resolver,
    cai na dim do provider configurado em settings.

    Não faz HTTP. Mudanças em settings só refletem após `apply_settings_to_env`
    (invalida get_settings.cache + `_embedder`/`_effective_provider`).

    Onda Q (2026-05-30): migrou de qdrant_store.py pra cá (backend-AGNÓSTICO).
    """
    provider = _effective_provider or (
        getattr(get_settings(), "embedding_provider", "azure") or "azure"
    ).lower()
    return _provider_dim(provider)


def embeddings_unavailable_detail() -> str:
    """Mensagem (para o 503 de ingestão) FIEL ao provider de embeddings ativo.

    Antes a mensagem era hardcoded "Azure", o que induzia ao erro quando o
    provider real era qwen3 (ex.: endpoint do hub inacessível → o usuário ia
    checar chaves Azure inexistentes). Aqui a mensagem reflete o provider de
    `settings.embedding_provider` e aponta para a config certa.
    """
    chain = _embedding_chain()
    provider = chain[0] if chain else "azure"
    cadeia = " → ".join(chain) if len(chain) > 1 else provider
    base = (
        f"Embeddings indisponível — nenhum provider da cadeia de roteamento "
        f"({cadeia}) respondeu."
    )
    if provider == "qwen3":
        return (
            base + " Verifique a URL/conectividade do endpoint qwen3 (Configurações "
            "→ qwen3_source / oss*_url) e, para o fallback, AZURE_OPENAI_API_KEY/ENDPOINT."
        )
    return base + " Verifique AZURE_OPENAI_API_KEY/ENDPOINT em Configurações."


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
    """Constroi o embedder do provider CONFIGURADO (sem fallback).

    Mantido por compat (testes + chamada direta). O runtime usa
    `_embed_with_fallback`, que roteia pela cadeia primário→fallback.
    """
    settings = get_settings()
    provider = (settings.embedding_provider or "azure").lower()
    if provider == "qwen3":
        return _build_qwen3_embedder()
    return _build_azure_embedder()


def _get_embedder():
    """Compat: embedder do provider configurado, cacheado. NÃO faz fallback —
    use embed_texts/embed_query para o roteamento resiliente."""
    global _embedder
    if _embedder is None:
        _embedder = _build_embedder()
    return _embedder


# ── Roteamento com fallback (paridade com a cadeia de resiliência do LLM) ──

_BUILDERS = {
    "qwen3": _build_qwen3_embedder,
    "azure": _build_azure_embedder,
}


def _embedding_chain() -> list[str]:
    """Ordem de tentativa: provider configurado, depois o fallback alcançável.

    Fallback default = 'azure' (provider primário, tipicamente acessível); se o
    configurado já é azure, tenta 'qwen3'. Operador pode fixar via
    settings.embedding_fallback_provider.
    """
    s = get_settings()
    primary = (getattr(s, "embedding_provider", "azure") or "azure").lower()
    fb = (getattr(s, "embedding_fallback_provider", "") or "").lower()
    if not fb:
        fb = "azure" if primary != "azure" else "qwen3"
    chain = [primary]
    if fb and fb != primary:
        chain.append(fb)
    return chain


async def _embed_call(emb, mode: str, payload):
    return await (
        emb.aembed_documents(payload) if mode == "documents" else emb.aembed_query(payload)
    )


async def _embed_with_fallback(mode: str, payload):
    """Roteia o embedding pela cadeia primário→fallback e cacheia o provider
    EFETIVO (o 1º que responde). Chamadas seguintes vão direto nele. Retorna
    None só se TODOS os providers da cadeia falharem.

    Transparência: ao usar o fallback, loga `event=embedding.fallback` (sempre,
    como a contingência de LLM — auditoria nunca é silenciada).
    """
    global _embedder, _effective_provider
    # Caminho rápido: já temos um embedder efetivo resolvido.
    if _embedder is not None and _effective_provider is not None:
        try:
            return await _embed_call(_embedder, mode, payload)
        except Exception as e:
            logger.warning(
                f"embedder efetivo '{_effective_provider}' falhou "
                f"({type(e).__name__}: {str(e)[:120]}); re-resolvendo a cadeia"
            )
            _embedder = None
            _effective_provider = None

    configured = (getattr(get_settings(), "embedding_provider", "azure") or "azure").lower()
    last_err: Optional[Exception] = None
    for prov in _embedding_chain():
        builder = _BUILDERS.get(prov)
        if builder is None:
            continue
        emb = builder()
        if emb is None:
            continue  # provider não configurado (ex.: Azure sem key) — tenta o próximo
        try:
            result = await _embed_call(emb, mode, payload)
        except Exception as e:
            last_err = e
            logger.warning(
                f"embedding provider '{prov}' indisponível: "
                f"{type(e).__name__}: {str(e)[:160]}"
            )
            continue
        # Sucesso — cacheia o efetivo. Loga fallback quando difere do configurado.
        if prov != configured:
            logger.warning(
                "embedding.fallback",
                extra={
                    "event": "embedding.fallback",
                    "from": configured,
                    "to": prov,
                    "reason": (type(last_err).__name__ if last_err else "primary_unavailable"),
                },
            )
        _embedder = emb
        _effective_provider = prov
        return result

    logger.warning(
        "embedding: todos os providers da cadeia falharam",
        extra={
            "event": "embedding.all_failed",
            "chain": _embedding_chain(),
            "last_error": (type(last_err).__name__ if last_err else None),
        },
    )
    return None


async def resolve_effective_provider() -> Optional[str]:
    """Resolve (e cacheia) qual provider de embedding realmente responde,
    sondando com um embed mínimo. Usado por fluxos que precisam da dimensão
    ATIVA ANTES de embedar de verdade — ex.: reindex recria a coluna pgvector
    com a dim do provider efetivo. No-op se já resolvido."""
    global _effective_provider
    if _effective_provider is not None:
        return _effective_provider
    await _embed_with_fallback("query", "ping")
    return _effective_provider


async def embed_texts(texts: list[str]) -> Optional[list[list[float]]]:
    """Gera embeddings em batch (com roteamento/fallback). Retorna None só se
    TODA a cadeia de providers falhar — aí o caller (ingest) reporta 503."""
    if not texts:
        return []
    return await _embed_with_fallback("documents", texts)


async def embed_query(text: str) -> Optional[list[float]]:
    """Embedding de uma query (single), com roteamento/fallback."""
    return await _embed_with_fallback("query", text)
