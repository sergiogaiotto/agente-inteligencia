"""Camada de dados — PostgreSQL via asyncpg.

API pública: `Repository`, `*_repo`, `settings_store`, `init_db`, `close_db`.

Decisões:
- Pool global (asyncpg.create_pool) inicializado em init_db().
- Repository delega no pool diretamente (sem context-manager por chamada).
- Migrações idempotentes via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- Booleanos persistidos como INTEGER 0/1 (legado de schema; trocar para
  BOOLEAN é refator de schema separado — quebraria checks `WHERE x = 1`).
"""

from __future__ import annotations

import re
import asyncio
import logging
from typing import Any, Optional

import asyncpg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Schema canônico (Postgres)
# ═══════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    urn TEXT UNIQUE,
    name TEXT NOT NULL,
    kind TEXT CHECK(kind IN ('orchestrator','router','subagent')) DEFAULT 'subagent',
    domain TEXT,
    version TEXT DEFAULT '0.1.0',
    stability TEXT CHECK(stability IN ('alpha','beta','stable','deprecated')) DEFAULT 'alpha',
    owner TEXT,
    purpose TEXT,
    activation_criteria TEXT,
    inputs_schema TEXT,
    workflow TEXT,
    tool_bindings TEXT DEFAULT '[]',
    output_contract TEXT,
    failure_modes TEXT,
    delegations TEXT,
    compensation TEXT,
    guardrails TEXT,
    budget TEXT DEFAULT '{}',
    examples TEXT DEFAULT '[]',
    telemetry TEXT,
    data_dependencies TEXT,
    model_constraints TEXT,
    evidence_policy TEXT,
    gold_refs TEXT,
    raw_content TEXT NOT NULL,
    content_hash TEXT,
    signed INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT CHECK(kind IN ('aobd','router','subagent')) DEFAULT 'subagent',
    domain TEXT,
    skill_id TEXT,
    llm_provider TEXT DEFAULT 'azure',
    model TEXT DEFAULT 'gpt-4o',
    system_prompt TEXT,
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active',
    version TEXT DEFAULT '1.0.0',
    temperature REAL DEFAULT 0.7,
    accepts_images INTEGER DEFAULT 0,
    accepts_documents INTEGER DEFAULT 0,
    require_evidence INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_bindings (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    model_serving_ref TEXT,
    mcp_servers TEXT DEFAULT '[]',
    config TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mesh_connections (
    id TEXT PRIMARY KEY,
    source_agent_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    connection_type TEXT DEFAULT 'sequential',
    config TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY,
    trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    origin_agent_id TEXT,
    origin_skill_urn TEXT,
    target_agent_id TEXT,
    target_skill_urn TEXT,
    intent TEXT,
    skill_ref TEXT,
    context TEXT DEFAULT '{}',
    state_pointer TEXT,
    budget_remaining TEXT DEFAULT '{}',
    deadline TEXT,
    status TEXT DEFAULT 'pending',
    signature TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS journeys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    domain TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at TIMESTAMP DEFAULT now(),
    ended_at TIMESTAMP,
    channel TEXT DEFAULT 'api',
    agent_id TEXT,
    customer_hash TEXT,
    journey_id TEXT,
    state TEXT DEFAULT 'Intake',
    release_id TEXT,
    metadata TEXT DEFAULT '{}',
    trace_data TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    turn_number INTEGER,
    user_text_redacted TEXT,
    output_text_redacted TEXT,
    interaction_id TEXT NOT NULL,
    envelope_id TEXT,
    latency_ms REAL DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    source_type TEXT,
    version TEXT,
    confidentiality_label TEXT DEFAULT 'internal',
    index_version TEXT,
    last_updated TEXT,
    authorized INTEGER DEFAULT 1,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY,
    snippet_id TEXT,
    snippet_text TEXT,
    relevance_score REAL,
    confidentiality_label TEXT,
    knowledge_source_id TEXT,
    turn_id TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mcp_server TEXT,
    mcp_server_type TEXT DEFAULT 'http',
    description TEXT,
    operations TEXT DEFAULT '[]',
    input_schema TEXT,
    output_schema TEXT,
    sla TEXT DEFAULT '{}',
    cost_per_call REAL DEFAULT 0,
    auth_requirements TEXT,
    auth_token TEXT,
    auth_config TEXT DEFAULT '{}',
    sensitivity TEXT DEFAULT 'internal',
    requires_trusted_context INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    tool_name TEXT,
    mcp_server TEXT,
    input_hash TEXT,
    output_hash TEXT,
    input_data TEXT,
    output_data TEXT,
    latency_ms REAL DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    interaction_id TEXT,
    tool_id TEXT,
    envelope_id TEXT,
    status TEXT DEFAULT 'completed',
    created_at TIMESTAMP DEFAULT now()
);

-- Bindings declarativos executados (1 row por chamada do engine, inclui
-- compensações). Permite queries como "latência média do binding X" ou
-- "qual binding falha mais nesta semana". Linka api_call_logs via call_id.
CREATE TABLE IF NOT EXISTS binding_executions (
    id TEXT PRIMARY KEY,
    interaction_id TEXT NOT NULL,
    agent_id TEXT DEFAULT '',
    binding_id TEXT NOT NULL,
    call_id TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    attempts INTEGER DEFAULT 1,
    error TEXT,
    skipped_by_breaker BOOLEAN DEFAULT FALSE,
    is_compensation BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    name TEXT,
    environment TEXT DEFAULT 'staging',
    model_config TEXT DEFAULT '{}',
    prompt_config TEXT DEFAULT '{}',
    index_config TEXT DEFAULT '{}',
    policy_config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'candidate',
    baseline_metrics TEXT DEFAULT '{}',
    released_at TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gold_cases (
    id TEXT PRIMARY KEY,
    dataset_version TEXT,
    case_type TEXT DEFAULT 'normal',
    journey TEXT,
    channel TEXT,
    complexity TEXT,
    input_text TEXT NOT NULL,
    expected_output TEXT NOT NULL,
    expected_state TEXT,
    -- Enriquecimento Golden Dataset (categoria semântica + ponderação + match flexível + sentinelas)
    category TEXT,
    weight REAL DEFAULT 1.0,
    expected_pattern TEXT,
    red_flags TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL,
    gold_version TEXT,
    run_type TEXT DEFAULT 'baseline',
    total_cases INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0,
    evidence_coverage REAL DEFAULT 0,
    correct_refusal_rate REAL DEFAULT 0,
    false_positive_rate REAL DEFAULT 0,
    avg_latency_ms REAL DEFAULT 0,
    avg_cost_usd REAL DEFAULT 0,
    details TEXT DEFAULT '[]',
    status TEXT DEFAULT 'running',
    gate_result TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS car_entries (
    id TEXT PRIMARY KEY,
    skill_urn TEXT NOT NULL,
    domain TEXT,
    activation_keywords TEXT DEFAULT '[]',
    required_entities TEXT DEFAULT '[]',
    actor_profile TEXT,
    jurisdiction TEXT,
    embedding_vector TEXT,
    success_rate REAL DEFAULT 1.0,
    latency_p95 REAL DEFAULT 0,
    avg_cost REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    details TEXT DEFAULT '{}',
    trace_id TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS drift_events (
    id TEXT PRIMARY KEY,
    release_id TEXT,
    metric_name TEXT,
    baseline_value REAL,
    current_value REAL,
    magnitude REAL,
    detection_method TEXT,
    severity TEXT DEFAULT 'warning',
    resolved INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS system_prompts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT DEFAULT 'geral',
    kind TEXT DEFAULT 'subagent',
    prompt_text TEXT NOT NULL,
    variables TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    version INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'comum',
    domains TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMP DEFAULT now()
);

-- API keys para integrações externas (Zapier, n8n, scripts, mobile apps).
-- Header `X-API-Key` é validado contra `key_hash` (SHA-256 da plaintext).
-- `key_prefix` é gravado pra UI exibir "ag_live_a1b2…" sem revelar o resto.
-- revoked_at preserva audit (vs DELETE — nunca remove o registro histórico).
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    last_used_at TIMESTAMP,
    revoked_at TIMESTAMP,
    expires_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS api_connectors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'AP',
    color TEXT DEFAULT 'bg-brand-500',
    api_key TEXT DEFAULT '',
    auth_type TEXT DEFAULT 'none',
    auth_header TEXT DEFAULT 'X-API-Key',
    health_path TEXT DEFAULT '/api/health',
    timeout_ms INTEGER DEFAULT 30000,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_endpoints (
    id TEXT PRIMARY KEY,
    connector_id TEXT NOT NULL,
    name TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT 'geral',
    sample_body TEXT DEFAULT '{}',
    sample_headers TEXT DEFAULT '{}',
    is_favorite INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_call_logs (
    id TEXT PRIMARY KEY,
    connector_id TEXT DEFAULT '',
    endpoint_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    request_headers TEXT DEFAULT '{}',
    request_body TEXT DEFAULT '{}',
    response_body TEXT DEFAULT '',
    status_code INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════
-- Onda 3 — RAG real: chunks de documentos ingeridos.
-- O vetor de embedding fica no Qdrant (collection `agente_evidence`),
-- indexado pelo mesmo `id` desta tabela. Aqui guardamos o texto cru
-- + tsvector gerado para BM25 nativo do Postgres.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS evidence_chunks (
    id TEXT PRIMARY KEY,
    knowledge_source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER,
    char_count INTEGER,
    -- Coluna gerada: tsvector mantido em sync automaticamente pelo Postgres.
    -- 'simple' não faz stemming agressivo — bom para mistura PT/EN sem dicionário.
    tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_chunks_tsv ON evidence_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_evidence_chunks_source ON evidence_chunks (knowledge_source_id);

-- ═══════════════════════════════════════════════════════════════
-- Verifier v2 — resultado do judge multi-dimensional + ContractValidator.
-- 1 linha por chamada de verify(). Permite query analítica posterior:
-- "qual dimensão falha mais?", "qual modelo é mais confiável?", drift detection.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    turn_id TEXT,
    interaction_id TEXT,
    -- Dimensões (NULL se não avaliadas — ex: profile fast pula judge)
    factuality_score REAL,
    factuality_reason TEXT,
    completeness_score REAL,
    completeness_reason TEXT,
    tone_score REAL,
    tone_reason TEXT,
    safety_score REAL,
    safety_reason TEXT,
    -- ContractValidator (determinístico, sem LLM)
    contract_compliant BOOLEAN,
    contract_errors TEXT DEFAULT '[]',
    -- Agregados
    ok BOOLEAN NOT NULL DEFAULT FALSE,
    confidence REAL,
    unsupported_claims TEXT DEFAULT '[]',
    -- Metadata
    judge_model TEXT,
    profile TEXT,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_verifications_turn ON verifications (turn_id);
CREATE INDEX IF NOT EXISTS idx_verifications_interaction ON verifications (interaction_id);
CREATE INDEX IF NOT EXISTS idx_verifications_created_at ON verifications (created_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- Catálogo / Marketplace corporativo (Onda 1)
-- Agentes, skills e (futuro) recipes/external_platforms publicáveis
-- com governança Root, capability disclosure e tracking de custo.
-- URN futuro-proof prevê multi-workspace e federação.
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS catalog_entries (
    id TEXT PRIMARY KEY,
    -- urn:maestro:<workspace>:<kind>:<slug>:<version> — workspace='default' na Onda 1.
    urn TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    kind TEXT NOT NULL CHECK(kind IN ('agent','skill','application','recipe','external_platform')),
    -- Vínculo com artefato Maestro existente (NULL para external_platform / recipe stub)
    artifact_type TEXT CHECK(artifact_type IN ('agent','skill','recipe')),
    artifact_id TEXT,
    domain TEXT,
    version TEXT DEFAULT '0.1.0',
    -- Lifecycle (state machine): draft → submitted → approved → published → deprecated → archived
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN (
        'draft','submitted','approved','published','deprecated','archived'
    )),
    -- Visibilidade R9: private (só owner+Root) | department (área) | company (toda)
    visibility TEXT NOT NULL DEFAULT 'private' CHECK(visibility IN ('private','department','company')),
    visibility_scope TEXT,  -- nome do departamento quando visibility='department'
    -- Stewardship R11
    owner_user_id TEXT NOT NULL,
    steward_team TEXT,
    -- Adapter binding R7 (a2a = agente/skill interno via protocolo A2A)
    adapter_type TEXT NOT NULL DEFAULT 'a2a' CHECK(adapter_type IN (
        'a2a','mcp','http','openai_assistants'
    )),
    adapter_config TEXT DEFAULT '{}',
    -- Trust metrics R5.2 (atualizadas via batch a partir de catalog_costs / harness)
    trust_reliability REAL DEFAULT 0,
    trust_latency_p95_ms REAL DEFAULT 0,
    trust_avg_cost_usd REAL DEFAULT 0,
    trust_invocation_count INTEGER DEFAULT 0,
    trust_last_invoked_at TIMESTAMP,
    tags TEXT DEFAULT '[]',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    published_at TIMESTAMP,
    deprecated_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_status ON catalog_entries(status);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_kind ON catalog_entries(kind);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_owner ON catalog_entries(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_catalog_entries_artifact ON catalog_entries(artifact_type, artifact_id);

-- Submissões: 1 row por submit. Entry pode ter múltiplas (re-submissão após changes_requested).
CREATE TABLE IF NOT EXISTS catalog_submissions (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES catalog_entries(id) ON DELETE CASCADE,
    submitted_by TEXT NOT NULL,
    submitted_at TIMESTAMP DEFAULT now(),
    -- Snapshot do estado da entry no instante da submissão (audit + diff futuro)
    snapshot TEXT DEFAULT '{}',
    -- Relatório de pré-checks automáticos (schema, secrets-scan, capability fingerprint, harness)
    precheck_report TEXT DEFAULT '{}',
    precheck_passed BOOLEAN DEFAULT FALSE,
    -- Decisão de revisão (Root na Onda 1)
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status IN (
        'pending','approved','rejected','changes_requested'
    )),
    reviewed_by TEXT,
    reviewed_at TIMESTAMP,
    review_notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_catalog_submissions_entry ON catalog_submissions(entry_id);
CREATE INDEX IF NOT EXISTS idx_catalog_submissions_status ON catalog_submissions(review_status);

-- Capability Disclosure R6.3 — "etiqueta nutricional" obrigatória (1:1 com entry).
CREATE TABLE IF NOT EXISTS catalog_capability_disclosure (
    entry_id TEXT PRIMARY KEY REFERENCES catalog_entries(id) ON DELETE CASCADE,
    reads_user_kb BOOLEAN DEFAULT FALSE,
    writes_user_kb BOOLEAN DEFAULT FALSE,
    calls_external_apis BOOLEAN DEFAULT FALSE,
    external_apis_list TEXT DEFAULT '[]',
    stores_input BOOLEAN DEFAULT FALSE,
    storage_retention_days INTEGER,
    accesses_internet BOOLEAN DEFAULT FALSE,
    processes_pii BOOLEAN DEFAULT FALSE,
    processes_financial BOOLEAN DEFAULT FALSE,
    processes_health BOOLEAN DEFAULT FALSE,
    trains_on_input BOOLEAN DEFAULT FALSE,
    output_is_deterministic BOOLEAN DEFAULT FALSE,
    -- Soberania R6.1 (NULL = sem restrição)
    data_residency TEXT,
    additional_notes TEXT DEFAULT '',
    -- Verificação (Onda 2: 'execution'; Onda 1 sempre 'declared')
    verified_at TIMESTAMP,
    verification_method TEXT DEFAULT 'declared' CHECK(verification_method IN (
        'declared','fingerprint','execution'
    )),
    declared_vs_detected TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- Tracking de custo por invocação R4.3 (insert-only; agregação fica em queries).
CREATE TABLE IF NOT EXISTS catalog_costs (
    id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES catalog_entries(id) ON DELETE CASCADE,
    consumer_user_id TEXT NOT NULL,
    consumer_department TEXT,
    interaction_id TEXT,
    cost_usd REAL DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    invoked_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_catalog_costs_entry ON catalog_costs(entry_id);
CREATE INDEX IF NOT EXISTS idx_catalog_costs_consumer ON catalog_costs(consumer_user_id);
CREATE INDEX IF NOT EXISTS idx_catalog_costs_invoked_at ON catalog_costs(invoked_at DESC);

-- ═══════════════════════════════════════════════════════════════
-- External Platforms metadata (Onda 2) — 1:1 com catalog_entries
-- quando kind='external_platform'. Cataloga IAs terceirizadas
-- aprovadas pela empresa (ChatGPT/Cursor/Copilot/etc.) com vendor,
-- contrato vigente, custo mensal, contatos, casos de uso aprovados.
-- Inventário de IA da empresa para governança e compliance.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS catalog_external_metadata (
    entry_id TEXT PRIMARY KEY REFERENCES catalog_entries(id) ON DELETE CASCADE,
    vendor TEXT NOT NULL,
    vendor_url TEXT,
    contract_status TEXT NOT NULL DEFAULT 'none' CHECK(contract_status IN (
        'none','negotiating','active','expired','terminated'
    )),
    contract_renewal_date DATE,
    monthly_cost_usd REAL,
    vendor_contact TEXT,
    approved_use_cases TEXT DEFAULT '',
    restrictions TEXT DEFAULT '',
    approved_by_user_id TEXT,
    approved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════
-- Recipes (Onda 3) — composição declarativa de entries existentes.
-- 1:1 com catalog_entries quando kind='recipe'. Steps são ordered
-- list em JSONB com referência a outras entries.
-- Onda 3 entrega apenas o manifest declarativo; execução real (chain
-- sequencial pelo engine) vem na Onda 4.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS catalog_recipes (
    entry_id TEXT PRIMARY KEY REFERENCES catalog_entries(id) ON DELETE CASCADE,
    -- Steps: [{"order":1, "target_entry_id":"...", "notes":"..."}, ...]
    steps JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- ═══════════════════════════════════════════════════════════════
-- Recipe Executions (Onda 4) — runs reais dos recipes publicados.
-- 1:N com catalog_entries (kind='recipe') via recipe_entry_id.
-- Chain mode: output[N-1] vira input[N]. Falha de N marca demais
-- como 'skipped' (status final = 'partial'). Async: POST cria com
-- status='running' e background task atualiza até 'completed'/'partial'/'failed'.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS catalog_recipe_executions (
    id TEXT PRIMARY KEY,
    recipe_entry_id TEXT NOT NULL REFERENCES catalog_entries(id) ON DELETE CASCADE,
    consumer_user_id TEXT NOT NULL,
    input TEXT NOT NULL,
    -- steps_results: [{order, target_entry_id, target_name, status:
    --   success|error|skipped, output, error, cost_usd, latency_ms,
    --   started_at, finished_at}, ...]
    steps_results JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN (
        'running','completed','partial','failed'
    )),
    total_cost_usd REAL DEFAULT 0,
    total_latency_ms INTEGER DEFAULT 0,
    error_message TEXT,
    started_at TIMESTAMP DEFAULT now(),
    finished_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_catalog_recipe_exec_recipe
    ON catalog_recipe_executions(recipe_entry_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_recipe_exec_consumer
    ON catalog_recipe_executions(consumer_user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_recipe_exec_status
    ON catalog_recipe_executions(status);

-- ═══════════════════════════════════════════════════════════════
-- Onda Tabular — promoção de CSV/XLSX a tabela consultável via SQL.
-- Metadata em Postgres; dados ficam em DuckDB embarcado (data/tabular/).
-- Skills consomem via seção `## Data Tables` no SKILL.md (declarative,
-- sem LLM gerando SQL — query parametrizada com bind vars seguras).
-- Visibility e confidentiality_label são herdadas da KS de origem.
-- ═══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS data_tables (
    id TEXT PRIMARY KEY,
    knowledge_source_id TEXT NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
    -- urn:table:<ks_short>:<slug>:<version> — referência estável em SKILL.md
    urn TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    -- Schema inferido: [{"name": "col", "type": "INTEGER|VARCHAR|DOUBLE|DATE|...", "nullable": true}, ...]
    schema_json JSONB NOT NULL DEFAULT '[]',
    row_count INTEGER DEFAULT 0,
    column_count INTEGER DEFAULT 0,
    size_bytes INTEGER DEFAULT 0,
    -- Caminho relativo do arquivo .duckdb (ex: "data/tabular/<ks_id>/<table_id>.duckdb")
    duckdb_path TEXT NOT NULL,
    -- Nome da tabela DENTRO do DuckDB (sempre "data" no MVP — 1 tabela por arquivo)
    duckdb_table_name TEXT DEFAULT 'data',
    version TEXT DEFAULT '1',
    -- Lifecycle: ingesting → ready → error → deleted
    status TEXT NOT NULL DEFAULT 'ingesting' CHECK(status IN ('ingesting','ready','error','deleted')),
    error_message TEXT,
    -- Score de "tabular_ready" calculado na análise (0.0-1.0). Auditável.
    quality_score REAL DEFAULT 0,
    -- PK inferida (única + não nula). NULL se não detectada.
    suggested_pk TEXT,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_data_tables_ks ON data_tables(knowledge_source_id);
CREATE INDEX IF NOT EXISTS idx_data_tables_status ON data_tables(status);

-- Auditoria 1 row por execução de query (paridade com api_call_logs).
-- Permite "qual skill consultou qual tabela", "queries que falharam",
-- "tempo médio por tabela". sql_rendered NÃO contém bind values
-- (evita PII em log); inputs_json sim, mas pode ser truncado/redactado.
CREATE TABLE IF NOT EXISTS data_table_query_logs (
    id TEXT PRIMARY KEY,
    data_table_id TEXT NOT NULL REFERENCES data_tables(id) ON DELETE CASCADE,
    -- Liga a invocação (paridade com api_call_logs.interaction_id)
    interaction_id TEXT,
    agent_id TEXT DEFAULT '',
    executed_by TEXT,
    -- SQL parametrizado SEM os bind values inline
    sql_rendered TEXT NOT NULL,
    -- Inputs do usuário (pode conter PII — tratar como dados sensíveis)
    inputs_json JSONB DEFAULT '{}',
    row_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','error','blocked')),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_data_table_query_logs_table ON data_table_query_logs(data_table_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_data_table_query_logs_interaction ON data_table_query_logs(interaction_id);
"""

# ═══════════════════════════════════════════════════════════════
# Migrações idempotentes (executadas após o schema)
# ═══════════════════════════════════════════════════════════════

_IDEMPOTENT_MIGRATIONS = [
    # Garante colunas adicionadas em versões posteriores ao schema base
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS title TEXT",
    "ALTER TABLE interactions ADD COLUMN IF NOT EXISTS trace_data TEXT DEFAULT '{}'",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS version TEXT DEFAULT '1.0.0'",
    "ALTER TABLE tools ADD COLUMN IF NOT EXISTS mcp_server_type TEXT DEFAULT 'http'",
    "ALTER TABLE tools ADD COLUMN IF NOT EXISTS auth_token TEXT",
    "ALTER TABLE tools ADD COLUMN IF NOT EXISTS auth_config TEXT DEFAULT '{}'",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS require_evidence INTEGER DEFAULT 1",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS temperature REAL DEFAULT 0.7",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS accepts_images INTEGER DEFAULT 0",
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS accepts_documents INTEGER DEFAULT 0",
    # Onda 7: paradigma de seleção LLM por task type. NULL = legacy (usa
    # llm_provider/model direto). Setado = resolve via app/llm_routing.py
    # consultando platform_settings.
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS task_type TEXT",
    # processing_message: humaniza o execution_log no painel de rastreabilidade.
    # Cada agente expõe uma frase de status (ex: "Orquestrando seu pedido",
    # "Escolhendo o especialista", "Pensando na sua consulta") que aparece no
    # topo do segmento do agente no log pós-execução. Zero impacto de perf —
    # injetado durante a montagem do log que já estava sendo construído.
    "ALTER TABLE agents ADD COLUMN IF NOT EXISTS processing_message TEXT",
    # Golden Dataset enriquecido — taxonomia, peso ponderado, match flexível, sentinelas
    "ALTER TABLE gold_cases ADD COLUMN IF NOT EXISTS category TEXT",
    "ALTER TABLE gold_cases ADD COLUMN IF NOT EXISTS weight REAL DEFAULT 1.0",
    "ALTER TABLE gold_cases ADD COLUMN IF NOT EXISTS expected_pattern TEXT",
    "ALTER TABLE gold_cases ADD COLUMN IF NOT EXISTS red_flags TEXT DEFAULT '[]'",
    # Harness multi-dim gate — agregados do Verifier por execução (§9.5 + §14.2)
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS avg_factuality REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS avg_completeness REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS avg_tone REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS safety_violation_rate REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS contract_compliance_rate REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS hallucination_rate REAL",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS judge_used BOOLEAN DEFAULT FALSE",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS judge_model TEXT",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS gate_reason TEXT",
    "ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS dimension_breakdown TEXT DEFAULT '{}'",
    # Tabela `traces` foi criada no schema mas nunca escrita por ninguém — traces
    # vivem no Tempo via OTLP. Drop idempotente p/ remover de instâncias antigas
    # (sempre vazia, sem dependentes).
    "DROP TABLE IF EXISTS traces",
    # api_call_logs.interaction_id: liga cada chamada HTTP do declarative engine
    # à invocação que a originou. Antes era inferido por (agent_id + janela
    # temporal) na UI — frágil sob concorrência. NULL em rows pré-migration.
    "ALTER TABLE api_call_logs ADD COLUMN IF NOT EXISTS interaction_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_api_call_logs_interaction ON api_call_logs(interaction_id)",
    # Índices da nova tabela binding_executions (lookups por interaction + analytics
    # cross-agent/cross-binding).
    "CREATE INDEX IF NOT EXISTS idx_binding_executions_interaction ON binding_executions(interaction_id)",
    "CREATE INDEX IF NOT EXISTS idx_binding_executions_agent_binding ON binding_executions(agent_id, binding_id)",
    # Onda 4 PR #70: sandbox flag em execuções de recipe. Runs sandbox NÃO
    # gravam em catalog_costs (free tier de dev) — flag distingue para
    # filtragem em UI e queries futuras.
    "ALTER TABLE catalog_recipe_executions ADD COLUMN IF NOT EXISTS is_sandbox BOOLEAN DEFAULT FALSE",
    # API Connectors — revisão de qualidade (2026-05):
    #   verify_ssl: permite desabilitar verificação TLS por connector
    #     (default ON; só desligue conscientemente para APIs com self-signed cert)
    #   body_type:  tipo de body que o endpoint envia. 'json' (default, retrocompat),
    #     'form_urlencoded', 'multipart', 'text', 'xml'. Sem isso só JSON era suportado.
    "ALTER TABLE api_connectors ADD COLUMN IF NOT EXISTS verify_ssl INTEGER DEFAULT 1",
    "ALTER TABLE api_endpoints ADD COLUMN IF NOT EXISTS body_type TEXT DEFAULT 'json'",
    # Catalog submissions — garantir FK ON DELETE CASCADE (2026-05).
    # Tabelas que vieram de versões antigas do schema podem ter FK sem CASCADE,
    # gerando submissions órfãs quando a entry é deletada. CREATE TABLE IF NOT
    # EXISTS não retroativa FKs em tabelas existentes — precisa do ALTER explícito.
    # DROP + ADD é idempotente em Postgres (IF EXISTS no DROP, e ADD recria limpo).
    "ALTER TABLE catalog_submissions DROP CONSTRAINT IF EXISTS catalog_submissions_entry_id_fkey",
    """ALTER TABLE catalog_submissions ADD CONSTRAINT catalog_submissions_entry_id_fkey
       FOREIGN KEY (entry_id) REFERENCES catalog_entries(id) ON DELETE CASCADE""",
    # Onda Tabular: kb_mode declara o tipo de conteúdo da KS.
    # - 'text': só RAG (textos, FAQs, contratos). Upload de planilha vira chunks markdown.
    # - 'tabular': só Tabelas DuckDB. Rejeita formatos não-estruturados. ZERO chunks no Qdrant/Postgres.
    # - 'hybrid' (default): comportamento legacy — aceita tudo, oferece promote a tabela.
    # Backfill = hybrid para KS existentes (preserva comportamento).
    "ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS kb_mode TEXT DEFAULT 'hybrid'",
    # Onda Tabular — coluna `data_tables` na tabela skills.
    # Bug reportado: criar skill nova pela UI estourava 500 com
    # `UndefinedColumnError: column "data_tables" of relation "skills"
    # does not exist`. O parser (skill_to_db_dict em app/skill_parser/parser.py)
    # já retornava `data_tables` no dict desde os PRs #110/#115, mas a DDL
    # da tabela skills (CREATE TABLE acima) não foi atualizada e nenhuma
    # migration idempotente cobriu o gap. Esta ALTER fecha o circuito —
    # idempotente, então safe em qualquer ambiente. TEXT default '' (vazio)
    # = compat com skills antigas que não tinham ## Data Tables.
    "ALTER TABLE skills ADD COLUMN IF NOT EXISTS data_tables TEXT DEFAULT ''",
    # ─── PR D pgvector foundation ───────────────────────────────
    # Extensão `vector` (pgvector). Idempotente; só funciona em imagens com
    # a extensão disponível (pgvector/pgvector:pg16 e similares). Postgres
    # vanilla sem pgvector instalado vai dar erro nesta migration — neste
    # caso, ou trocar imagem ou comentar esta linha (RAG vetorial cai em
    # qdrant via Settings.rag_vector_backend=qdrant). A coluna `embedding`
    # NÃO é criada aqui — fica a cargo de pgvector_store.ensure_embedding_column()
    # que sabe a dim do provider de embedding ativo em runtime
    # (Azure 1536, Qwen3 1024 etc.). Mudar provider = /reindex recria coluna.
    "CREATE EXTENSION IF NOT EXISTS vector",
]


# ═══════════════════════════════════════════════════════════════
# Pool global
# ═══════════════════════════════════════════════════════════════

_pool: Optional[asyncpg.Pool] = None
_init_lock = asyncio.Lock()


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError(
            "Pool PostgreSQL não inicializado. Confirme que init_db() foi chamado "
            "no startup (lifespan) e que DATABASE_URL aponta para um Postgres acessível."
        )
    return _pool


# ═══════════════════════════════════════════════════════════════
# Helpers — splitter de scripts SQL e conversor de placeholders
# ═══════════════════════════════════════════════════════════════

_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_]*\$")


def _split_sql(script: str) -> list[str]:
    """Divide um script SQL em statements individuais.

    Respeita strings ('...', "..."), strings duplicadas (''), dollar-quoting
    ($tag$ ... $tag$) e comentários single-line (`-- ... \\n`).

    Comentários single-line são preservados na saída (PG aceita) mas seu
    conteúdo NÃO é tokenizado — `;` ou `'` dentro de `-- comment` ficam
    inertes. Sem isso, um comentário como `-- foo; bar` cortaria o
    statement no meio.

    Comentários multilinha (/* ... */) não são suportados; o SCHEMA não usa.
    """
    out: list[str] = []
    cur: list[str] = []
    in_str = False
    sc: Optional[str] = None
    in_dollar = False
    dollar_tag: Optional[str] = None
    i = 0
    n = len(script)
    while i < n:
        c = script[i]
        if in_dollar:
            cur.append(c)
            if dollar_tag and script.startswith(dollar_tag, i):
                cur.extend(script[i + 1 : i + len(dollar_tag)])
                i += len(dollar_tag)
                in_dollar = False
                dollar_tag = None
                continue
            i += 1
            continue
        if in_str:
            cur.append(c)
            if c == sc:
                if i + 1 < n and script[i + 1] == sc:
                    cur.append(sc)
                    i += 2
                    continue
                in_str = False
                sc = None
            i += 1
            continue
        # Comentário single-line `-- ... \n` — copia inerte até o newline.
        # Evita que `;` ou `'` dentro do comentário disparem split/string.
        if c == "-" and i + 1 < n and script[i + 1] == "-":
            while i < n and script[i] != "\n":
                cur.append(script[i])
                i += 1
            continue
        if c in ("'", '"'):
            in_str = True
            sc = c
            cur.append(c)
            i += 1
            continue
        if c == "$":
            m = _DOLLAR_TAG_RE.match(script, i)
            if m:
                dollar_tag = m.group(0)
                cur.append(dollar_tag)
                i += len(dollar_tag)
                in_dollar = True
                continue
        if c == ";":
            stmt = "".join(cur).strip()
            if stmt:
                out.append(stmt)
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    last = "".join(cur).strip()
    if last:
        out.append(last)
    return out


# ═══════════════════════════════════════════════════════════════
# Init / shutdown
# ═══════════════════════════════════════════════════════════════

async def _init_pool_connection(conn):
    """Callback executado em CADA conexão nova do pool.

    Registra o codec `pgvector.asyncpg` se a lib estiver disponível — isso
    permite que asyncpg serialize/deserialize `vector` nativamente
    (`await conn.fetch("... WHERE embedding <=> $1", numpy_array)` etc.).

    Idempotente e tolerante a falhas:
    - Se pgvector lib não está instalada → no-op (RAG vetorial cai em qdrant).
    - Se a extensão `vector` não está criada no Postgres → register_vector
      lança InvalidArgumentError. Logamos warning e seguimos (a connection
      ainda funciona pra queries sem vetor).
    """
    try:
        from pgvector.asyncpg import register_vector
    except ImportError:
        return  # lib opcional; sem ela, rag_vector_backend=pgvector simplesmente não funciona
    try:
        await register_vector(conn)
    except Exception as e:
        logger.warning(
            "pgvector codec não registrado nesta conexão — extensão vector pode não estar criada",
            extra={
                "event": "pgvector.codec.register_failed",
                "error_type": type(e).__name__,
            },
        )


async def init_db():
    """Cria pool, aplica schema e migrações idempotentes.

    Seguro para chamar múltiplas vezes — usa lock + check.
    """
    global _pool
    settings = get_settings()
    async with _init_lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                min_size=settings.database_pool_min,
                max_size=settings.database_pool_max,
                command_timeout=60,
                init=_init_pool_connection,
            )
            logger.info(f"PostgreSQL pool aberto: min={settings.database_pool_min} max={settings.database_pool_max}")
        async with _pool.acquire() as con:
            for stmt in _split_sql(SCHEMA):
                await con.execute(stmt)
            for migration in _IDEMPOTENT_MIGRATIONS:
                try:
                    await con.execute(migration)
                except Exception as e:
                    logger.debug(f"Migration ignorada: {migration[:60]} — {e}")


async def close_db():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("PostgreSQL pool fechado.")


# ═══════════════════════════════════════════════════════════════
# Repository — CRUD genérico tabela-a-tabela
# ═══════════════════════════════════════════════════════════════


class Repository:
    """CRUD genérico para uma tabela em PostgreSQL via asyncpg."""

    def __init__(self, table: str):
        self.table = table

    async def _order_col(self, con: asyncpg.Connection) -> str:
        rows = await con.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = $1",
            self.table,
        )
        cols = {r["column_name"] for r in rows}
        for candidate in ("created_at", "started_at", "id"):
            if candidate in cols:
                return candidate
        return "id"

    async def find_all(self, limit: int = 100, offset: int = 0, **filters) -> list[dict]:
        pool = _get_pool()
        async with pool.acquire() as con:
            params: list[Any] = []
            cl: list[str] = []
            for k, v in filters.items():
                params.append(v)
                cl.append(f"{k}=${len(params)}")
            where = f" WHERE {' AND '.join(cl)}" if cl else ""
            order = await self._order_col(con)
            params.extend([limit, offset])
            sql = (
                f"SELECT * FROM {self.table}{where} "
                f"ORDER BY {order} DESC LIMIT ${len(params)-1} OFFSET ${len(params)}"
            )
            rows = await con.fetch(sql, *params)
            return [dict(r) for r in rows]

    async def find_by_id(self, id: str) -> Optional[dict]:
        pool = _get_pool()
        async with pool.acquire() as con:
            r = await con.fetchrow(f"SELECT * FROM {self.table} WHERE id=$1", id)
            return dict(r) if r else None

    async def create(self, data: dict) -> dict:
        pool = _get_pool()
        async with pool.acquire() as con:
            keys = list(data.keys())
            cols = ", ".join(keys)
            phs = ", ".join(f"${i+1}" for i in range(len(keys)))
            sql = f"INSERT INTO {self.table} ({cols}) VALUES ({phs})"
            await con.execute(sql, *[data[k] for k in keys])
            return data

    async def update(self, id: str, data: dict) -> Optional[dict]:
        if not data:
            return await self.find_by_id(id)
        pool = _get_pool()
        async with pool.acquire() as con:
            keys = list(data.keys())
            sets = ", ".join(f"{k}=${i+1}" for i, k in enumerate(keys))
            sql = f"UPDATE {self.table} SET {sets} WHERE id=${len(keys)+1}"
            await con.execute(sql, *[data[k] for k in keys], id)
            r = await con.fetchrow(f"SELECT * FROM {self.table} WHERE id=$1", id)
            return dict(r) if r else None

    async def delete(self, id: str) -> bool:
        pool = _get_pool()
        async with pool.acquire() as con:
            res = await con.execute(f"DELETE FROM {self.table} WHERE id=$1", id)
            # asyncpg devolve "DELETE n"
            try:
                n = int(res.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                n = 0
            return n > 0

    async def count(self, **filters) -> int:
        pool = _get_pool()
        async with pool.acquire() as con:
            params: list[Any] = []
            cl: list[str] = []
            for k, v in filters.items():
                params.append(v)
                cl.append(f"{k}=${len(params)}")
            where = f" WHERE {' AND '.join(cl)}" if cl else ""
            sql = f"SELECT COUNT(*) FROM {self.table}{where}"
            return await con.fetchval(sql, *params) or 0

    async def search(self, query: str, columns: list[str]) -> list[dict]:
        pool = _get_pool()
        async with pool.acquire() as con:
            order = await self._order_col(con)
            cl = " OR ".join(f"{c} ILIKE ${i+1}" for i, c in enumerate(columns))
            params = [f"%{query}%"] * len(columns)
            sql = f"SELECT * FROM {self.table} WHERE {cl} ORDER BY {order} DESC"
            rows = await con.fetch(sql, *params)
            return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# Repositórios singletons
# ═══════════════════════════════════════════════════════════════

skills_repo = Repository("skills")
agents_repo = Repository("agents")
bindings_repo = Repository("agent_bindings")
mesh_repo = Repository("mesh_connections")
envelopes_repo = Repository("envelopes")
journeys_repo = Repository("journeys")
interactions_repo = Repository("interactions")
turns_repo = Repository("turns")
knowledge_repo = Repository("knowledge_sources")
evidences_repo = Repository("evidences")
evidence_chunks_repo = Repository("evidence_chunks")  # Onda 3 — chunks de docs ingeridos
data_tables_repo = Repository("data_tables")  # Onda Tabular — metadata de tabelas DuckDB
data_table_query_logs_repo = Repository("data_table_query_logs")  # Onda Tabular — auditoria de queries
verifications_repo = Repository("verifications")  # §14.2 — resultado do Verifier multi-dim
tools_repo = Repository("tools")
tool_calls_repo = Repository("tool_calls")
binding_executions_repo = Repository("binding_executions")
releases_repo = Repository("releases")
gold_cases_repo = Repository("gold_cases")
eval_runs_repo = Repository("eval_runs")
car_repo = Repository("car_entries")
audit_repo = Repository("audit_log")
drift_repo = Repository("drift_events")
prompts_repo = Repository("system_prompts")
users_repo = Repository("users")
domains_repo = Repository("domains")
api_connectors_repo = Repository("api_connectors")
api_endpoints_repo = Repository("api_endpoints")
api_call_logs_repo = Repository("api_call_logs")
api_keys_repo = Repository("api_keys")

# Catálogo / Marketplace (Onda 1)
catalog_entries_repo = Repository("catalog_entries")
catalog_submissions_repo = Repository("catalog_submissions")
catalog_disclosure_repo = Repository("catalog_capability_disclosure")
catalog_costs_repo = Repository("catalog_costs")
# Onda 4: execuções reais de recipes (PK = id; helpers especializados em queries.py)
catalog_recipe_executions_repo = Repository("catalog_recipe_executions")


# ═══════════════════════════════════════════════════════════════
# Settings store (key-value para configurações da plataforma)
# ═══════════════════════════════════════════════════════════════


class SettingsStore:
    """Key-value store para configurações da plataforma."""

    async def get_all(self) -> dict:
        pool = _get_pool()
        async with pool.acquire() as con:
            rows = await con.fetch("SELECT key, value FROM platform_settings")
            return {r["key"]: r["value"] for r in rows}

    async def get(self, key: str, default: str = "") -> str:
        pool = _get_pool()
        async with pool.acquire() as con:
            v = await con.fetchval("SELECT value FROM platform_settings WHERE key=$1", key)
            return v if v is not None else default

    async def set(self, key: str, value: str):
        pool = _get_pool()
        async with pool.acquire() as con:
            await con.execute(
                "INSERT INTO platform_settings (key, value, updated_at) VALUES ($1, $2, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                key,
                value,
            )

    async def set_many(self, data: dict):
        pool = _get_pool()
        async with pool.acquire() as con:
            async with con.transaction():
                for k, v in data.items():
                    await con.execute(
                        "INSERT INTO platform_settings (key, value, updated_at) VALUES ($1, $2, now()) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                        k,
                        str(v),
                    )


settings_store = SettingsStore()
