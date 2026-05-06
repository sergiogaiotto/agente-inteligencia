"""Configuração central da aplicação."""

from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "AgenteInteligência-AI"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_key: str = "change-me"

    # ── Database (PostgreSQL) ──
    # Formato: postgresql://user:password@host:port/database
    # Em docker-compose: postgresql://agente:agente@postgres:5432/agente_inteligencia
    database_url: str = "postgresql://agente:agente@localhost:5432/agente_inteligencia"
    database_pool_min: int = 2
    database_pool_max: int = 10

    # ── Cache / Redis (memória de contexto) ──
    redis_url: str = "redis://localhost:6379/0"

    # ── Vector DB (Qdrant) ──
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "agente_evidence"

    # ── LLM provider primário ──
    # azure | openai | maritaca | ollama
    default_llm_provider: str = "azure"

    # ── Azure OpenAI (provedor principal) ──
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_version: str = "2024-02-15-preview"
    azure_openai_chat_deployment: str = "gpt-4o"
    azure_openai_embeddings_deployment: str = "text-embedding-3-small"

    # ── OpenAI (fallback) ──
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"

    # ── Maritaca AI ──
    maritaca_api_key: str = ""
    maritaca_api_url: str = "https://chat.maritaca.ai/api"
    maritaca_model: str = "sabia-3"

    # ── Ollama (local) ──
    ollama_api_url: str = "http://187.77.46.137:11434"
    ollama_api_key: str = ""
    ollama_model: str = "hf.co/Althayr/Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest"

    # ── Observabilidade ──
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── DeepAgent Harness ──
    deepagent_enabled: bool = True
    deepagent_max_iterations: int = 25
    deepagent_timeout: int = 120

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
