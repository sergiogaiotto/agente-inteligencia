"""Provedores de LLM — Azure OpenAI (primário), OpenAI, Maritaca, Ollama.

Onda 4b: Quando `settings.llm_gateway_enabled=True`, todos os providers passam
a usar o LiteLLM gateway em vez do upstream direto. Centraliza:
- Rate-limit (LiteLLM tem rate-limit nativo por modelo/key)
- Fallback automático Azure→OpenAI
- Custo unificado e tracing via LangFuse (callback do gateway)

Backward compat absoluto: contrato `LLMProvider.generate(messages) -> dict`
inalterado. Nenhum dos 9 callers de `get_provider()` precisa mudar.

Modos:
- Direct (default, gateway_enabled=false): comportamento original.
- Gateway: ChatOpenAI aponta para `llm_gateway_url` com `model="<provider>/<name>"`.
  LiteLLM faz o roteamento real (key, endpoint, api_version).
- Defesa em profundidade: se gateway 5xx/unreachable e fallback_to_direct=true,
  provider tenta upstream direto antes de propagar erro.
"""

import logging
import httpx
from abc import ABC, abstractmethod
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.core.config import get_settings

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """Interface base para provedores de LLM."""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> dict:
        ...

    @abstractmethod
    def get_langchain_llm(self):
        ...


# ───────────────────────────────────────────────────────────────
# Helper: monta um ChatOpenAI apontando para o gateway LiteLLM
# ───────────────────────────────────────────────────────────────
def _build_gateway_llm(model_name: str, temperature: float):
    """ChatOpenAI conectado ao LiteLLM. `model_name` deve incluir o prefixo
    do provider (ex: 'azure/gpt-4o', 'openai/gpt-4o', 'maritaca/sabia-3').
    """
    settings = get_settings()
    return ChatOpenAI(
        model=model_name,
        api_key=settings.llm_gateway_master_key,
        base_url=f"{settings.llm_gateway_url.rstrip('/')}/v1",
        temperature=temperature,
        # Timeout um pouco mais alto que upstream — gateway adiciona ~10ms
        timeout=180,
    )


def _gateway_active() -> bool:
    """Gateway só conta como ativo se enabled=true E master_key estiver setada."""
    s = get_settings()
    return s.llm_gateway_enabled and bool(s.llm_gateway_master_key)


# ───────────────────────────────────────────────────────────────
# Azure OpenAI
# ───────────────────────────────────────────────────────────────
class AzureOpenAIProvider(LLMProvider):
    """Provedor primário — Azure OpenAI Service.

    Diferenças vs OpenAI público:
    - URL única por deployment: <endpoint>/openai/deployments/<deployment>
    - api_version obrigatória (ex: 2024-02-15-preview)
    - `model` no factory é interpretado como `azure_deployment` quando
      passado; do contrário usa AZURE_OPENAI_CHAT_DEPLOYMENT do env.
    """

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.deployment = model or settings.azure_openai_chat_deployment
        self.model = self.deployment
        self.temperature = temperature
        self._gateway_mode = _gateway_active()

        if self._gateway_mode:
            # Modo gateway: nome de modelo com prefixo azure/<deployment>.
            self._llm = _build_gateway_llm(f"azure/{self.deployment}", temperature)
            self._direct_llm = None  # construído lazy se precisar de fallback
        else:
            self.endpoint = settings.azure_openai_endpoint
            self.api_key = settings.azure_openai_api_key
            self.api_version = settings.azure_openai_api_version
            if not self.endpoint or not self.api_key:
                self._llm = None
                return
            self._llm = AzureChatOpenAI(
                azure_endpoint=self.endpoint,
                azure_deployment=self.deployment,
                api_version=self.api_version,
                api_key=self.api_key,
                temperature=self.temperature,
            )

    def _build_direct_llm(self):
        """Lazy build do cliente upstream (usado só em fallback gateway→direct)."""
        s = get_settings()
        if not (s.azure_openai_endpoint and s.azure_openai_api_key):
            return None
        return AzureChatOpenAI(
            azure_endpoint=s.azure_openai_endpoint,
            azure_deployment=self.deployment,
            api_version=s.azure_openai_api_version,
            api_key=s.azure_openai_api_key,
            temperature=self.temperature,
        )

    def get_langchain_llm(self):
        if self._llm is None:
            raise RuntimeError(
                "Azure OpenAI não configurado. Defina AZURE_OPENAI_ENDPOINT e "
                "AZURE_OPENAI_API_KEY no .env."
            )
        return self._llm

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        return await _generate_via_langchain(self, messages, **kwargs)


# ───────────────────────────────────────────────────────────────
# OpenAI público — REMOVIDO (Onda 7 Wave 5).
# Provider "openai" agora é alias de AzureOpenAIProvider em get_provider().
# Toda chamada usa AZURE_OPENAI_API_KEY. Pra reabilitar OpenAI público
# direto, restaurar OpenAIProvider class + reinstanciar openai_api_key
# em config.Settings.
# ───────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────
# Maritaca AI
# ───────────────────────────────────────────────────────────────
class MaritacaProvider(LLMProvider):
    """Provedor Maritaca AI via HTTP direto OU via gateway."""

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.maritaca_model
        self.api_key = settings.maritaca_api_key
        self.api_url = settings.maritaca_api_url
        self.temperature = temperature
        self._gateway_mode = _gateway_active()
        if self._gateway_mode:
            self._llm = _build_gateway_llm(f"maritaca/{self.model}", temperature)
        else:
            self._llm = None  # path direto usa httpx, não langchain

    def _build_direct_llm(self):
        # Para Maritaca, fallback direto = httpx (gerenciado em generate()).
        return None

    def get_langchain_llm(self):
        if self._gateway_mode and self._llm is not None:
            return self._llm
        # Path original: ChatOpenAI com base_url Maritaca
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        if self._gateway_mode:
            return await _generate_via_langchain(self, messages, **kwargs)
        # Path direto: httpx (preserva comportamento original)
        return await self._generate_direct(messages, **kwargs)

    async def _generate_direct(self, messages: list[dict], **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self.api_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    **kwargs,
                },
            )
            return _parse_openai_compatible_response(response, provider="maritaca", model=self.model)


# ───────────────────────────────────────────────────────────────
# Ollama
# ───────────────────────────────────────────────────────────────
class OllamaProvider(LLMProvider):
    """Provedor Ollama via endpoint OpenAI-compatível (/v1/chat/completions)."""

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.ollama_model
        self.api_url = settings.ollama_api_url.rstrip("/")
        self.api_key = settings.ollama_api_key or "ollama"
        self.temperature = temperature
        self._gateway_mode = _gateway_active()
        if self._gateway_mode:
            self._llm = _build_gateway_llm(f"ollama/{self.model}", temperature)
        else:
            self._llm = None

    def _build_direct_llm(self):
        return None

    def get_langchain_llm(self):
        if self._gateway_mode and self._llm is not None:
            return self._llm
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        if self._gateway_mode:
            return await _generate_via_langchain(self, messages, **kwargs)
        return await self._generate_direct(messages, **kwargs)

    async def _generate_direct(self, messages: list[dict], **kwargs) -> dict:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.api_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature,
                    **kwargs,
                },
            )
            return _parse_openai_compatible_response(response, provider="ollama", model=self.model)


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
async def _generate_via_langchain(provider, messages: list[dict], **kwargs) -> dict:
    """Path comum para providers que usam LangChain (Azure / OpenAI / Gateway).

    Inclui defesa em profundidade: se gateway_mode=True E falhar com 5xx/connection,
    tenta cliente direto (montado lazy via provider._build_direct_llm()).
    """
    llm = provider.get_langchain_llm()
    lc_messages = []
    for m in messages:
        if m["role"] == "system":
            lc_messages.append(SystemMessage(content=m["content"]))
        elif m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    try:
        response = await llm.ainvoke(lc_messages)
    except Exception as e:
        if getattr(provider, "_gateway_mode", False) and get_settings().llm_gateway_fallback_to_direct:
            logger.warning(
                f"Gateway LLM falhou ({type(e).__name__}: {str(e)[:120]}); "
                f"tentando upstream direto como fallback"
            )
            direct = provider._build_direct_llm()
            if direct is None:
                raise
            response = await direct.ainvoke(lc_messages)
        else:
            raise

    return {
        "content": response.content,
        "model": provider.model,
        "usage": (response.response_metadata or {}).get("token_usage", {}),
    }


def _parse_openai_compatible_response(response, provider: str, model: str) -> dict:
    """Parse seguro de respostas OpenAI-compatíveis."""
    try:
        data = response.json()
    except Exception:
        raise RuntimeError(
            f"{provider}: resposta inválida (status {response.status_code}, "
            f"body[:200]={response.text[:200]!r})"
        )

    if response.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or response.text[:300])
        raise RuntimeError(f"{provider} HTTP {response.status_code}: {msg}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        err = data.get("error") if isinstance(data, dict) else None
        msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or "campo 'choices' ausente")
        raise RuntimeError(f"{provider}: {msg} (model={model})")

    try:
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"{provider}: estrutura inesperada em choices[0].message.content ({e})")

    return {
        "content": content or "",
        "model": data.get("model") or model,
        "usage": data.get("usage", {}),
    }


def get_provider(provider_name: str = "azure", **kwargs) -> LLMProvider:
    """Factory de provedores. Default: azure (Azure OpenAI Service).

    Onda 7 Wave 4: 'openai' vira ALIAS de 'azure'. OpenAIProvider público
    é deprecated — toda chamada que vinha "openai" agora resolve pra
    Azure usando azure_openai_api_key. Compatível com agentes legacy
    sem necessidade de migração.
    """
    providers = {
        "azure": AzureOpenAIProvider,
        "openai": AzureOpenAIProvider,  # alias — Onda 7 Wave 4
        "maritaca": MaritacaProvider,
        "ollama": OllamaProvider,
    }
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provedor '{provider_name}' não suportado. Use: {list(providers.keys())}")
    return provider_class(**kwargs)
