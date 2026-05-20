"""Provedores de LLM — Azure OpenAI (primário), Maritaca, Ollama.

Cada provider expõe `generate(messages) -> dict` e `get_langchain_llm()`.

Histórico:
- Onda 4b introduziu LiteLLM como gateway intermediário entre app e providers.
- Removido depois (gateway era opt-in e nunca chegou a virar default — overhead
  de container + RAM no VPS não compensava o roteamento simples por prefixo
  que `get_provider()` já entrega nativamente).
- OpenAI público também foi removido (Onda 7 Wave 5): provider 'openai' é
  alias de Azure pra retrocompat de agentes legacy.
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
# Maritaca AI — endpoint OpenAI-compatível
# ───────────────────────────────────────────────────────────────
class MaritacaProvider(LLMProvider):
    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.maritaca_model
        self.api_key = settings.maritaca_api_key
        self.api_url = settings.maritaca_api_url
        self.temperature = temperature

    def get_langchain_llm(self):
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        # Path httpx direto preserva controle fino sobre headers/timeout.
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
# Ollama — endpoint OpenAI-compatível (/v1/chat/completions)
# ───────────────────────────────────────────────────────────────
class OllamaProvider(LLMProvider):
    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.ollama_model
        self.api_url = settings.ollama_api_url.rstrip("/")
        self.api_key = settings.ollama_api_key or "ollama"
        self.temperature = temperature

    def get_langchain_llm(self):
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
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
# GPT-OSS (open-weight 20b / 120b via endpoint OpenAI-compatible)
# ───────────────────────────────────────────────────────────────
class GPTOSSProvider(LLMProvider):
    """Provedor para gpt-oss-20b e gpt-oss-120b.

    Cada size tem URL/api_key/model próprias em platform_settings —
    permite que cada modelo seja servido por endpoint dedicado (ex: hub
    interno com 2 GPUs distintas atendendo o 20b vs 120b).

    'not-needed' é valor válido de api_key — o proxy autentica de outra
    forma (rede interna, mTLS, etc.). Continua mandando o header
    Authorization Bearer pra compatibilidade com o cliente OpenAI.
    """

    def __init__(self, size: str = "120b", model: str | None = None, temperature: float = 0.7):
        if size not in ("20b", "120b"):
            raise ValueError(f"GPTOSSProvider size deve ser '20b' ou '120b' (got: {size!r})")
        settings = get_settings()
        self.size = size
        if size == "20b":
            self.api_url = (settings.oss20b_url or "").rstrip("/")
            self.api_key = settings.oss20b_api_key or "not-needed"
            self.model = model or settings.oss20b_model
        else:
            self.api_url = (settings.oss120b_url or "").rstrip("/")
            self.api_key = settings.oss120b_api_key or "not-needed"
            self.model = model or settings.oss120b_model
        self.temperature = temperature
        self.timeout = settings.llm_timeout_seconds

    def get_langchain_llm(self):
        if not self.api_url:
            return None
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.api_url,
            temperature=self.temperature,
            timeout=self.timeout,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        if not self.api_url:
            raise RuntimeError(
                f"gpt-oss-{self.size}: URL não configurada. "
                f"Configure em /settings → Plataforma → GPT-OSS."
            )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_url}/chat/completions",
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
            return _parse_openai_compatible_response(
                response, provider=f"gpt-oss-{self.size}", model=self.model,
            )


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
async def _generate_via_langchain(provider, messages: list[dict], **kwargs) -> dict:
    """Path comum para providers que usam LangChain (Azure)."""
    llm = provider.get_langchain_llm()
    lc_messages = []
    for m in messages:
        if m["role"] == "system":
            lc_messages.append(SystemMessage(content=m["content"]))
        elif m["role"] == "user":
            lc_messages.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            lc_messages.append(AIMessage(content=m["content"]))

    response = await llm.ainvoke(lc_messages)

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

    'openai' é ALIAS de 'azure' (Onda 7 Wave 4) — toda chamada que vinha como
    "openai" resolve pra Azure usando azure_openai_api_key. Compatível com
    agentes legacy sem necessidade de migração.
    """
    providers = {
        "azure": AzureOpenAIProvider,
        "openai": AzureOpenAIProvider,  # alias
        "maritaca": MaritacaProvider,
        "ollama": OllamaProvider,
    }
    # gpt-oss tem 2 sizes com URL/key próprias — distinguir via factory
    if provider_name == "gpt-oss-20b":
        return GPTOSSProvider(size="20b", **kwargs)
    if provider_name == "gpt-oss-120b":
        return GPTOSSProvider(size="120b", **kwargs)
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provedor '{provider_name}' não suportado. Use: {list(providers.keys()) + ['gpt-oss-20b', 'gpt-oss-120b']}")
    return provider_class(**kwargs)
