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
    # Onda 7 Wave 5: OPENAI_API_KEY pública removida. Provider "openai" foi
    # transformado em alias de Azure OpenAI em llm_providers.get_provider —
    # toda chamada usa AZURE_OPENAI_API_KEY. Para acesso direto ao OpenAI
    # público (sem Azure), reabilitar reinstanciando OpenAIProvider e
    # esta config; mas é raro o caso em produção empresarial.

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

    # ── Verifier v2 (judge multi-dimensional + ContractValidator) ──
    # Promove EvidenceChecker (Onda 0) a 1ª classe, separando RAG de Verification.
    # OFF por default → comportamento legacy preservado (_LegacyVerifier roda no lugar).
    verifier_v2_enabled: bool = False
    # Modelo do juiz. Anti-self-preference: idealmente um provider ≠ do gerador.
    # Formato "<provider>/<model>" ou apenas <model> (assume azure).
    verifier_judge_model: str = "azure/gpt-4o"
    # Thresholds de aprovação por dimensão (escala 0-5). 3.0 = 60% proficiência.
    verifier_factuality_threshold: float = 3.0
    verifier_completeness_threshold: float = 3.0
    verifier_tone_threshold: float = 3.0
    # Cap de tokens da resposta do juiz. ~600 cobre 4 dimensões + claims sem cortar.
    verifier_max_tokens: int = 800

    # ── Verifier production mode (async sampling) ──
    # Quando True E verifier_v2_enabled True, o branch verifier do engine não
    # bloqueia mais a resposta: sample_rate% das interações são julgadas em
    # background. Resposta segue com heurística rasa (evidence_score). Útil em
    # produção — 100% sync é caro (1 LLM call extra) e lento (+2-4s).
    # Defaults conservadores: OFF até ligar explicitamente.
    verifier_production_async: bool = False
    verifier_production_sample_rate: float = 0.10  # 10% das interações
    verifier_max_concurrent_jobs: int = 20  # backpressure: drop acima disso

    # ── Harness multi-dim gate (§9.5 + §14.2) ──
    # Quando True, run_evaluation re-julga cada caso via Verifier (profile=rigorous)
    # e gate combina accuracy/refusal/FP com avg_factuality/safety/contract.
    # Toggle off → harness opera no modo legacy (proxy raso por shape).
    harness_use_verifier: bool = True
    harness_min_accuracy: float = 0.80
    harness_min_avg_factuality: float = 3.5
    harness_min_avg_completeness: float = 3.0
    harness_min_avg_tone: float = 3.0
    harness_max_safety_violation_rate: float = 0.05
    harness_min_contract_compliance: float = 0.95
    harness_max_hallucination_rate: float = 0.10
    harness_max_dim_regression_pct: float = 5.0

    # ── Policy Engine (Onda 4a — OPA Policy as Code) ──
    # Quando True, decisões sensíveis (PolicyCheck, tool invocation) consultam o
    # OPA em opa_url e seguem a decisão. Auditoria via audit_log.
    # Default OFF: comportamento idêntico ao de hoje, zero risco.
    opa_enabled: bool = False
    opa_url: str = "http://opa:8181"
    # Failsafe-open: se OPA offline, allow=true com warning + audit. Default em dev.
    # Trocar para False em produção com dados sensíveis (failsafe-closed = nega na falha).
    opa_failsafe_open: bool = True
    # Timeout curto: OPA local é ~1ms. Acima disso é problema, e app não pode esperar.
    opa_timeout_seconds: float = 2.0

    # ── RAG v2 (Onda 3 — Qdrant + embeddings reais) ──
    # Toggle global. Quando False, retriever cai no fallback antigo (busca textual
    # em metadados de knowledge_sources). Quando True E há chunks ingeridos, usa
    # busca híbrida BM25 (Postgres tsvector) + vetorial (Qdrant), fundidos via RRF.
    rag_v2_enabled: bool = True
    # Tokens por chunk e overlap entre chunks consecutivos. 500/50 é sweet spot
    # para text-embedding-3-small. Aumentar exige mais contexto/custo no LLM final.
    rag_chunk_size_tokens: int = 500
    rag_chunk_overlap_tokens: int = 50
    # Top-N de cada perna antes da fusão. RRF então reduz para top_n do retriever (default 5).
    rag_top_n_vector: int = 20
    rag_top_n_bm25: int = 20
    # Constante k do Reciprocal Rank Fusion. 60 é o default da literatura.
    rag_rrf_k: int = 60
    # Quando True: pós-RRF, manda os top-N para o LLM reordenar com justificativa.
    # Trade-off: +500ms latência, +$0.0005/query, mas qualidade superior.
    # Quando False: usa heurística de overlap de termos (mais rápido, menos preciso).
    rag_rerank_with_llm: bool = True
    # Encoding do tiktoken — cl100k_base cobre GPT-4 / GPT-3.5 / text-embedding-3-*.
    rag_tiktoken_encoding: str = "cl100k_base"

    # ── Observabilidade self-hosted (Onda 2 — OTel + Tempo + Loki + Grafana) ──
    # Default OFF: instrumentação só liga quando `OTEL_ENABLED=true` no .env e
    # o profile `full` do docker-compose estiver ativo (`docker compose --profile full up`).
    # Quando OFF, init_otel() é no-op e nenhuma dep OTel é exercitada em runtime.
    otel_enabled: bool = False
    otel_service_name: str = "agente-inteligencia"
    otel_service_version: str = "2.0.0"
    # Endpoint OTLP gRPC do Tempo (4317 é a porta padrão OTLP/gRPC).
    # Em docker-compose: tempo:4317. Local fora do compose: localhost:4317.
    otel_exporter_otlp_endpoint: str = "http://tempo:4317"
    # parentbased_always_on (default em dev) | parentbased_traceidratio (prod com OTEL_TRACES_SAMPLER_ARG=0.1)
    otel_traces_sampler: str = "parentbased_always_on"
    # Endpoint Loki (não usado pelo app — Promtail tail dos logs do Docker; mantido para futura
    # integração de log handler nativo, se quisermos emitir logs direto via push API).
    loki_endpoint: str = "http://loki:3100"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


# ═══════════════════════════════════════════════════════════════
# UI override → env vars
# ═══════════════════════════════════════════════════════════════
# Settings persistidas em platform_settings (settings_store) sobrescrevem
# os valores do .env em runtime. Estratégia: lê banco → popula os.environ
# → invalida lru_cache de get_settings() → invalida singleton de embedder.
# Próximas chamadas de get_settings() leem os env vars atualizados.
#
# Chamado em 2 momentos:
#  - lifespan startup do FastAPI (após init_db)
#  - PUT /settings (após set_many)
#
# Ausência de valor (string vazia ou chave faltando) NÃO sobrescreve env —
# preserva o .env como fallback de boot.
# ═══════════════════════════════════════════════════════════════

# Mapa chave-do-banco → nome-da-env-var. Pydantic é case-insensitive,
# então AZURE_OPENAI_API_KEY lê do mesmo lugar que azure_openai_api_key.
_UI_TO_ENV_MAP = {
    # Azure OpenAI (provedor primário)
    "azure_key":                  "AZURE_OPENAI_API_KEY",
    "azure_endpoint":             "AZURE_OPENAI_ENDPOINT",
    "azure_api_version":          "AZURE_OPENAI_API_VERSION",
    "azure_chat_deployment":      "AZURE_OPENAI_CHAT_DEPLOYMENT",
    "azure_embeddings_deployment":"AZURE_OPENAI_EMBEDDINGS_DEPLOYMENT",
    # Maritaca AI
    "maritaca_key":  "MARITACA_API_KEY",
    "maritaca_url":  "MARITACA_API_URL",
    "maritaca_model":"MARITACA_MODEL",
    # Ollama
    "ollama_url":    "OLLAMA_API_URL",
    "ollama_model":  "OLLAMA_MODEL",
    # LangFuse (observabilidade SaaS opcional)
    "langfuse_public":"LANGFUSE_PUBLIC_KEY",
    "langfuse_secret":"LANGFUSE_SECRET_KEY",
    "langfuse_host":  "LANGFUSE_HOST",
    # NOTA: openai_key/openai_model não mapeiam — provider 'openai' virou
    # alias de Azure na Onda 7 Wave 5. Mantidos no settings_store apenas
    # pra retrocompat de UI (card OpenAI continua mostrando os campos).
}


async def apply_settings_to_env() -> int:
    """Lê settings_store (Postgres) e popula os.environ com valores não-vazios.

    Invalida caches downstream (get_settings.lru_cache, _embedder singleton)
    pra que próxima leitura pegue os valores novos sem restart.

    Retorna o número de chaves aplicadas. 0 se banco indisponível ou tudo vazio.
    """
    import os
    try:
        # Import tardio pra evitar ciclo (database importa get_settings).
        from app.core.database import settings_store
        data = await settings_store.get_all()
    except Exception:
        # Banco offline ou tabela ainda não criada (init_db não rodou).
        return 0

    applied = 0
    for ui_key, env_name in _UI_TO_ENV_MAP.items():
        val = data.get(ui_key)
        if val and str(val).strip():
            os.environ[env_name] = str(val).strip()
            applied += 1

    # Invalida cache pra próxima chamada de get_settings() rebuild com novas envs
    get_settings.cache_clear()

    # Invalida singleton do embedder (instância já existente está com creds antigas)
    try:
        from app.evidence import embedder as _emb
        _emb._embedder = None
    except Exception:
        pass

    return applied


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Defesa contra config errada: rate alto + async ligado sinaliza no log.
    # @lru_cache garante que isso só roda uma vez por processo.
    if s.verifier_production_async and s.verifier_production_sample_rate > 0.5:
        import logging
        logging.getLogger(__name__).warning(
            f"VERIFIER_PRODUCTION_SAMPLE_RATE={s.verifier_production_sample_rate} "
            "está alto (>50%). Custo de LLM extra pode ser proibitivo. "
            "Considere reduzir se isso não for intencional."
        )
    return s
