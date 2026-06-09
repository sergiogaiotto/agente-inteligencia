"""Configuração central da aplicação."""

import logging

from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # ── App ──
    app_name: str = "Maestro"
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

    # ── Vector DB ──
    # Onda Q (2026-05-30): backend único pgvector (Postgres com pgvector
    # extension). Qdrant removido. Settings qdrant_url/api_key/collection
    # + rag_vector_backend removidas — não há mais escolha de backend.

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
    # Onda 7 Wave 5: OPENAI_API_KEY pública foi virada alias de Azure (provider
    # "openai" resolve pra Azure). Mantido como retrocompat de agentes legacy.
    #
    # MUDANÇA 2026-05-29 PR #194 (user pediu): OpenAI público REAL reintroduzido
    # como provider separado `openai_public`. Não substitui o alias antigo —
    # convive. Usado quando operador quer chamar api.openai.com diretamente
    # (ex: rotear skill_generation pro gpt-4o público em vez do Azure pra
    # comparar latência/custo).
    openai_public_api_key: str = ""
    openai_public_base_url: str = "https://api.openai.com/v1"
    openai_public_model: str = "gpt-4o"

    # ── Maritaca AI ──
    maritaca_api_key: str = ""
    maritaca_api_url: str = "https://chat.maritaca.ai/api"
    maritaca_model: str = "sabia-3"

    # ── Ollama (local) ──
    ollama_api_url: str = "http://187.77.46.137:11434"
    ollama_api_key: str = ""
    ollama_model: str = "hf.co/Althayr/Gemma-3-Gaia-PT-BR-4b-it-GGUF:latest"

    # ── GPT-OSS (open-weight via endpoint OpenAI-compatible) ──
    # 2 modelos com URL/key próprias — provedor interno serve cada um em
    # endpoint dedicado. "not-needed" como api_key é válido (proxy autentica
    # de outra forma, ex: rede interna).
    oss120b_url: str = ""
    oss120b_model: str = "openai/gpt-oss-120b"
    oss120b_api_key: str = ""
    oss20b_url: str = ""
    oss20b_model: str = "openai/gpt-oss-20b"
    oss20b_api_key: str = ""
    llm_timeout_seconds: int = 300

    # ── Modelo Primário (fallback global) ──
    # Usado quando agent não tem task_type (Roteamento LLM da Onda 7) NEM
    # snapshot próprio de llm_provider/model. Quando definido, substitui o
    # default histórico — agents legacy sem primary caem em gpt-oss-120b.
    primary_provider: str = ""  # ex: "gpt-oss-120b" | "azure" | "maritaca" | "ollama"
    primary_model: str = ""     # ex: "openai/gpt-oss-120b" | "gpt-4o" | "sabia-4"

    # ── Idioma de resposta (fallback global) ──
    # Aplicado quando agent.response_language está vazio. Engine prepende
    # instrução explícita no system_prompt — LLM responde no idioma escolhido
    # mesmo quando contexto/evidências estão em outros idiomas (caso típico:
    # busca Tavily retorna inglês, mas resposta sai em pt-BR).
    # Formato: tag IETF BCP-47 ("pt-BR", "en-US", "es-ES"). UI mostra label
    # humano via _LANGUAGE_LABELS em llm_providers (mapeamento futuro).
    default_response_language: str = "pt-BR"

    # ── Embedding provider (Qwen3 | Azure) ──
    # Default: Qwen3 (open-weight via hub interno). Reusa URL/key do OSS source
    # escolhido (oss20b ou oss120b), só muda o path. Endpoint efetivo:
    # <scheme>://<host_do_OSS>/<qwen3_path>  →  ex: https://hub-gpus.claro.com.br/embed06b/v1
    embedding_provider: str = "qwen3"  # 'qwen3' | 'azure'
    qwen3_source: str = "oss120b"      # 'oss120b' | 'oss20b'
    qwen3_path: str = "embed06b/v1"
    qwen3_model: str = "Qwen/Qwen3-Embedding-0.6B"
    # Densidade do vetor (Matryoshka): truncamento server-side da dim do output.
    # 0 = não envia o parâmetro (usa default do modelo: 1024 para Qwen3-Embedding-0.6B).
    # Mudar exige re-embedar a collection do Qdrant — a dim precisa bater entre
    # write e read. Trocar em produção sem plano quebra a busca.
    qwen3_dimensions: int = 0

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

    # ── Grounded-by-default (2026-06-06) ──
    # Princípio global: o conhecimento paramétrico do modelo NUNCA é usado para
    # compor respostas — o agente responde SÓ com base em evidências (anexos,
    # RAG, resultados de tools). Quando True (default), o engine (1) injeta uma
    # diretiva estrita de grounding no system prompt e (2) faz o VerifyEvidence
    # RECUSAR respostas sem nenhuma evidência (anexo/RAG/tool/pipeline). Escape
    # hatch por agente: allow_general_knowledge=1 (ex: brainstorming). Override
    # global via env GROUNDING_STRICT=false ou Settings UI. Ver engine.py
    # (_build_grounding_directive + _grounding_guard).
    grounding_strict: bool = True

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

    # ── Contract retry on failure (Wave atual) ──
    # Quando ContractValidator marca compliant=false, Verifier re-chama o LLM
    # 1x com instrução de correção (incluindo os erros específicos). Custo:
    # 1 chamada LLM extra na falha. Ganho: muitas violações de formato são
    # triviais (vírgula sobrando, chave faltando) — o retry corrige sem
    # operador intervir. Default ON (qualidade > custo); desligue em casos
    # extremos de orçamento apertado.
    verifier_contract_retry_enabled: bool = True
    # Cap de tokens da resposta do retry. Maior que o do judge (800) porque
    # aqui o LLM regenera o draft completo, não só uma avaliação.
    verifier_contract_retry_max_tokens: int = 2000

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """SSOT: o arquivo .env NUNCA fornece credenciais/seleção de modelo.

        Pedido do operador (2026-06-06): todos os modelos da plataforma —
        provedores (Azure, OpenAI público, Maritaca, Ollama, GPT-OSS 120b/20b),
        embedding (Qwen3/Azure), Modelo Primário e Langfuse — devem usar
        EXCLUSIVAMENTE as chaves/acessos da tela de Configurações (persistidos
        em platform_settings e aplicados a os.environ por apply_settings_to_env).
        O .env passa a ser ignorado para esses campos.

        Implementação: filtra as chaves seladas (_SEALED_ENV_VARS) da fonte
        dotenv. Assim, mesmo que a chave exista no arquivo .env, ela não entra
        em Settings — cai no default da classe quando os.environ não a tiver.

        Precedência (inalterada): init > env (os.environ, escrito pela tela via
        apply_settings_to_env) > dotenv(FILTRADO) > secrets > defaults. Campos
        fora do escopo (infra, flags de segurança, default_llm_provider,
        grounding_strict, idioma) continuam lendo o .env normalmente.
        """
        def sealed_dotenv_settings():
            raw = dotenv_settings()
            return {
                key: value
                for key, value in raw.items()
                if key.upper() not in _SEALED_ENV_VARS
            }

        return (init_settings, env_settings, sealed_dotenv_settings, file_secret_settings)


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
# Regra por escopo (SSOT de modelos, 2026-06-06):
#  - Chaves SELADAS (_SEALED_ENV_VARS: provedores, embedding, primário, Langfuse):
#    valor no banco → escreve em os.environ; banco vazio → REMOVE de os.environ
#    (apaga resíduo injetado pelo docker env_file) pra cair no default da classe.
#    O .env nunca alimenta essas chaves.
#  - Demais chaves (grounding_strict, default_response_language): valor no banco
#    sobrescreve; ausência NÃO mexe em os.environ — preserva o .env como fallback.
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
    # OpenAI público (api.openai.com) — PR #194
    "openai_public_api_key":  "OPENAI_PUBLIC_API_KEY",
    "openai_public_base_url": "OPENAI_PUBLIC_BASE_URL",
    "openai_public_model":    "OPENAI_PUBLIC_MODEL",
    # GPT-OSS (open-weight via endpoint OpenAI-compatible)
    "oss120b_url":     "OSS120B_URL",
    "oss120b_model":   "OSS120B_MODEL",
    "oss120b_api_key": "OSS120B_API_KEY",
    "oss20b_url":      "OSS20B_URL",
    "oss20b_model":    "OSS20B_MODEL",
    "oss20b_api_key":  "OSS20B_API_KEY",
    "llm_timeout_seconds": "LLM_TIMEOUT_SECONDS",
    # Modelo Primário (fallback global)
    "primary_provider": "PRIMARY_PROVIDER",
    "primary_model":    "PRIMARY_MODEL",
    # Idioma de resposta global (BCP-47: pt-BR, en-US, ...)
    "default_response_language": "DEFAULT_RESPONSE_LANGUAGE",
    # Grounded-by-default: 'true'/'false'. Desliga a recusa global de respostas
    # sem evidência (não recomendado — fura o princípio anti-alucinação).
    "grounding_strict": "GROUNDING_STRICT",
    # MCP per-tool (D): 'true'/'false'. Liga o modo em que cada tool MCP vira sua
    # própria função com o inputSchema real (vs o legado {operation, query}).
    # Default OFF; lido a cada chamada por runtime.per_tool_enabled().
    "mcp_per_tool_enabled": "MCP_PER_TOOL_ENABLED",
    # Embedding (Qwen3 reusa URL/key do OSS source)
    "embedding_provider": "EMBEDDING_PROVIDER",
    "qwen3_source":       "QWEN3_SOURCE",
    "qwen3_path":         "QWEN3_PATH",
    "qwen3_model":        "QWEN3_MODEL",
    "qwen3_dimensions":   "QWEN3_DIMENSIONS",
    # LangFuse (observabilidade SaaS opcional)
    "langfuse_public":"LANGFUSE_PUBLIC_KEY",
    "langfuse_secret":"LANGFUSE_SECRET_KEY",
    "langfuse_host":  "LANGFUSE_HOST",
    # NOTA: openai_key/openai_model não mapeiam — provider 'openai' virou
    # alias de Azure na Onda 7 Wave 5. Mantidos no settings_store apenas
    # pra retrocompat de UI (card OpenAI continua mostrando os campos).
}


# ═══════════════════════════════════════════════════════════════
# SSOT de modelos: env vars SELADAS (o .env é ignorado para elas)
# ═══════════════════════════════════════════════════════════════
# Pedido do operador (2026-06-06): a tela de Configurações é a ÚNICA fonte de
# verdade para credenciais/seleção de modelo. Estas env vars vêm só do banco
# (platform_settings → os.environ via apply_settings_to_env) ou do default da
# classe — NUNCA do .env.
#
# Subconjunto NÃO-modelo de _UI_TO_ENV_MAP: chaves que continuam podendo vir do
# .env porque não são credencial/seleção de modelo. Tudo o mais no mapa é selado.
_NON_MODEL_UI_KEYS = {
    "grounding_strict",          # flag de comportamento anti-alucinação
    "default_response_language", # idioma de resposta global (BCP-47)
    "mcp_per_tool_enabled",      # flag do modo per-tool MCP (default OFF)
}

# Cobre: Azure, OpenAI público, Maritaca, Ollama, GPT-OSS 120b/20b, embedding
# (Qwen3/Azure), Modelo Primário (provider/model + timeout) e Langfuse.
# Usado em 2 lugares: (1) Settings.settings_customise_sources filtra estas chaves
# da fonte dotenv; (2) apply_settings_to_env remove resíduos do .env de os.environ
# quando o banco não tem valor — forçando o default da classe.
_SEALED_ENV_VARS = frozenset(
    env_name
    for ui_key, env_name in _UI_TO_ENV_MAP.items()
    if ui_key not in _NON_MODEL_UI_KEYS
)


async def apply_settings_to_env() -> int:
    """Aplica as settings do banco (tela de Configurações) a os.environ.

    SSOT de modelos (2026-06-06): para as chaves SELADAS (_SEALED_ENV_VARS),
    esta função é AUTORITATIVA sobre os.environ:
      - valor não-vazio no banco → escreve em os.environ (a tela vence);
      - banco vazio/ausente → REMOVE de os.environ qualquer resíduo (ex: o
        docker injeta o .env inteiro via env_file no boot) pra que Settings caia
        no default da classe — o .env nunca alimenta essas chaves.
    Para as demais chaves do mapa (não-modelo: grounding_strict, idioma), mantém
    o comportamento legado: só sobrescreve quando há valor; ausência preserva o
    .env como fallback de boot.

    Invalida caches downstream (get_settings.lru_cache, _embedder singleton) pra
    que a próxima leitura pegue os valores novos sem restart.

    Retorna o número de chaves aplicadas (escritas). 0 se banco indisponível.
    """
    import os
    try:
        # Import tardio pra evitar ciclo (database importa get_settings).
        from app.core.database import settings_store
        data = await settings_store.get_all()
    except Exception:
        # Banco offline ou tabela ainda não criada (init_db não rodou). Não dá
        # pra selar sem o banco — loga pra troubleshooting e mantém o boot.
        logger.warning(
            "event=settings.apply_skipped reason=store_unavailable "
            "detail='settings_store.get_all() falhou; seal de modelos NÃO aplicado'",
            exc_info=True,
        )
        return 0

    applied = 0
    removed = 0
    for ui_key, env_name in _UI_TO_ENV_MAP.items():
        val = data.get(ui_key)
        if val is not None and str(val).strip():
            os.environ[env_name] = str(val).strip()
            applied += 1
        elif env_name in _SEALED_ENV_VARS:
            # Selada e sem valor no banco → remove resíduo do .env de os.environ
            # (injetado pelo docker env_file) pra cair no default da classe.
            if os.environ.pop(env_name, None) is not None:
                removed += 1

    logger.info(
        "event=settings.model_seal applied=%d removed=%d sealed_total=%d",
        applied, removed, len(_SEALED_ENV_VARS),
    )

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
