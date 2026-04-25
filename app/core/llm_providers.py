"""Provedores de LLM — OpenAI e Maritaca AI."""

import httpx
import json
from abc import ABC, abstractmethod
from langchain_openai import ChatOpenAI
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


class OpenAIProvider(LLMProvider):
    """Provedor OpenAI via LangChain."""

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
        settings = get_settings()
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
            data = response.json()
            return {
                "content": data["choices"][0]["message"]["content"],
                "model": self.model,
                "usage": data.get("usage", {}),
            }


class OllamaProvider(LLMProvider):
    """Provedor Ollama via endpoint OpenAI-compatível (/v1/chat/completions).

    Ollama expõe API compatível com OpenAI nativamente — basta apontar o
    `base_url` para `<host>/v1`. API key é aceita como qualquer string
    (geralmente "ollama" por convenção).
    """

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
            data = response.json()
            return {
                "content": data["choices"][0]["message"]["content"],
                "model": self.model,
                "usage": data.get("usage", {}),
            }


def get_provider(provider_name: str = "openai", **kwargs) -> LLMProvider:
    """Factory de provedores."""
    providers = {
        "openai": OpenAIProvider,
        "maritaca": MaritacaProvider,
        "ollama": OllamaProvider,
    }
    provider_class = providers.get(provider_name)
    if not provider_class:
        raise ValueError(f"Provedor '{provider_name}' não suportado. Use: {list(providers.keys())}")
    return provider_class(**kwargs)
