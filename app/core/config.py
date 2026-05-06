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

    # ── Rate-limit (Onda 1) ──
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    # Limites por janela. *_per_min é interpretado como "por janela".
    rate_limit_default_per_min: int = 60       # rotas API genéricas
    rate_limit_workspace_per_min: int = 20     # rotas que disparam LLM
    rate_limit_auth_per_min: int = 10          # /login (anti-brute-force)
    # Cap de tokens por interação — proteção LLM04 contra runaway loops
    interaction_max_tokens: int = 80000

    # ── Auth hardening (Onda 1) ──
    # bcrypt sempre ativo; SHA256 legado validado e migrado no próximo login.
    # CSRF default OFF para não quebrar frontend antes de adaptado — ligar
    # quando o JS adicionar `X-CSRF-Token` em todos POST/PUT/DELETE.
    csrf_required: bool = False
    cookie_secure: bool = False                # True em produção HTTPS
    cookie_samesite: str = "lax"               # "lax" | "strict" | "none"
    session_max_age_seconds: int = 7 * 24 * 3600

    # ── DLP / PII redaction (Onda 1) ──
    dlp_enabled: bool = True
    # Se True, aplica redaction também ANTES de enviar prompt ao LLM (perde
    # contexto de identificadores reais). Default False — só redacta na
    # persistência (cumpre LLM06 sem prejudicar a UX).
    dlp_redact_before_llm: bool = False

    # ── Prompt injection guard (Onda 1, LLM01) ──
    prompt_guard_enabled: bool = True
    # Score 0..1: bloqueia interação inteira se >= block_threshold
    prompt_guard_block_threshold: float = 0.7
    # Score 0..1: registra warning em audit_log mas deixa passar
    prompt_guard_warn_threshold: float = 0.4

    # ── Prompt leak guard (Onda 1, LLM10) ──
    # Em traces de retorno, mostra apenas hash + preview do system_prompt em vez
    # do texto cru. Admin pode obter o original via rota dedicada (futuro).
    prompt_leak_guard_enabled: bool = True
    prompt_leak_preview_chars: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
