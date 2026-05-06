"""Provedores de LLM — Azure OpenAI (primário), OpenAI, Maritaca, Ollama."""

import httpx
from abc import ABC, abstractmethod
from langchain_openai import ChatOpenAI, AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from app.core.config import get_settings


class LLMProvider(ABC):
    """Interface base para provedores de LLM."""

    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> dict:
        ...

    @abstractmethod
    def get_langchain_llm(self):
        ...


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
        # No Azure, "model" significa "deployment name" — exposto no portal.
        self.deployment = model or settings.azure_openai_chat_deployment
        self.model = self.deployment  # exposto pra logs/trace
        self.temperature = temperature
        self.endpoint = settings.azure_openai_endpoint
        self.api_key = settings.azure_openai_api_key
        self.api_version = settings.azure_openai_api_version

        if not self.endpoint or not self.api_key:
            # Cria sem inicializar para permitir _llm fallback de erro tratado
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
        llm = self.get_langchain_llm()
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
            "model": self.model,
            "usage": (response.response_metadata or {}).get("token_usage", {}),
        }


class OpenAIProvider(LLMProvider):
    """Provedor OpenAI público (fallback)."""

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.openai_model
        self.api_key = settings.openai_api_key
        self.temperature = temperature
        self._llm = ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            temperature=self.temperature,
        )

    def get_langchain_llm(self):
        return self._llm

    async def generate(self, messages: list[dict], **kwargs) -> dict:
        lc_messages = []
        for m in messages:
            if m["role"] == "system":
                lc_messages.append(SystemMessage(content=m["content"]))
            elif m["role"] == "user":
                lc_messages.append(HumanMessage(content=m["content"]))
            elif m["role"] == "assistant":
                lc_messages.append(AIMessage(content=m["content"]))

        response = await self._llm.ainvoke(lc_messages)
        return {
            "content": response.content,
            "model": self.model,
            "usage": response.response_metadata.get("token_usage", {}),
        }


class MaritacaProvider(LLMProvider):
    """Provedor Maritaca AI via HTTP direto."""

    def __init__(self, model: str | None = None, temperature: float = 0.7):
        settings = get_settings()
        self.model = model or settings.maritaca_model
        self.api_key = settings.maritaca_api_key
        self.api_url = settings.maritaca_api_url
        self.temperature = temperature

    def get_langchain_llm(self):
        # Maritaca usa endpoint compatível com OpenAI
        return ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=f"{self.api_url}/v1",
            temperature=self.temperature,
        )

    async def generate(self, messages: list[dict], **kwargs) -> dict:
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


class OllamaProvider(LLMProvider):
    """Provedor Ollama via endpoint OpenAI-compatível (/v1/chat/completions)."""

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
    """Factory de provedores. Default: azure (Azure OpenAI Service)."""
    providers = {
        "azure": AzureOpenAIProvider,
        "openai": OpenAIProvider,
        "maritaca": MaritacaProvider,
        "ollama": OllamaProvider,
    }
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provedor '{provider_name}' não suportado. Use: {list(providers.keys())}")
    return provider_class(**kwargs)
