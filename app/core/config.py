"""Configuração central da aplicação."""

from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    app_name: str = "AgenteInteligência-AI"
    app_env: str = "development"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    secret_key: str = "change-me"

    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR}/data/agente_inteligencia.db"

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1"

    maritaca_api_key: str = ""
    maritaca_api_url: str = "https://chat.maritaca.ai/api"
    maritaca_model: str = "sabia-3"

    ollama_api_url: str = "http://187.77.46.137:32768"
    ollama_api_key: str = ""
    ollama_model: str = "Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest"

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    deepagent_enabled: bool = True
    deepagent_max_iterations: int = 25
    deepagent_timeout: int = 120

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
