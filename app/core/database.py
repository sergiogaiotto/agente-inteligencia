"""Camada de dados — PostgreSQL via asyncpg.

Mantém a API pública (`Repository`, `get_db`, `*_repo`, `settings_store`)
idêntica à anterior (que usava aiosqlite) — todo o código existente em
routes/, agents/, evidence/, harness/ continua funcionando sem alteração.

Decisões:
- Pool global (asyncpg.create_pool) inicializado em init_db().
- Repository delega no pool diretamente (sem context-manager por chamada).
- get_db() é mantido como wrapper compat para o código que usa SQL bruto
  com placeholders '?' (estilo SQLite). _ConnCompat converte '?' → '$N'
  no momento da execução.
- Schema convertido de SQLite para Postgres preservando nomes/colunas.
- Migrações idempotentes via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- Booleanos permanecem como INTEGER 0/1 para compatibilidade com o código
  existente que escreve/lê inteiros (signed=0, status=1, etc).
"""

from __future__ import annotations

import re
import asyncio
import logging
from contextlib import asynccontextmanager
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

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    trace_id TEXT,
    interaction_id TEXT,
    spans TEXT DEFAULT '[]',
    duration_ms REAL DEFAULT 0,
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

    Respeita strings ('...', "..."), strings duplicadas ('') e dollar-quoting
    ($tag$ ... $tag$). Não tenta entender comentários multilinha — aceitável
    porque o SCHEMA aqui não usa.
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


def _qmark_to_dollar(sql: str) -> str:
    """Converte placeholders '?' (SQLite-style) para '$1, $2, ...' (Postgres)."""
    out: list[str] = []
    n = 0
    in_str = False
    sc: Optional[str] = None
    i = 0
    while i < len(sql):
        c = sql[i]
        if in_str:
            out.append(c)
            if c == sc:
                if i + 1 < len(sql) and sql[i + 1] == sc:
                    out.append(sc)
                    i += 2
                    continue
                in_str = False
                sc = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = True
            sc = c
            out.append(c)
            i += 1
            continue
        if c == "?":
            n += 1
            out.append(f"${n}")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ═══════════════════════════════════════════════════════════════
# Init / shutdown
# ═══════════════════════════════════════════════════════════════

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
# get_db() — wrapper compat para código que usa SQL bruto com '?'
# ═══════════════════════════════════════════════════════════════


class _CursorCompat:
    """Cursor-like wrapper para retornar resultados ao estilo aiosqlite."""

    def __init__(self, rows: list):
        self._rows = rows

    async def fetchall(self) -> list:
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None


class _ConnCompat:
    """Conexão wrapper que aceita SQL com '?' (estilo aiosqlite).

    Usada por código legado que ainda chama `db.execute("...?...", params)` /
    `db.executescript(...)` / `db.commit()`. Toda nova rota deve usar o pool
    asyncpg diretamente via Repository — este wrapper existe apenas para
    compatibilidade durante a transição.
    """

    def __init__(self, con: asyncpg.Connection):
        self._con = con

    async def execute(self, sql: str, params=None) -> _CursorCompat:
        sql_pg = _qmark_to_dollar(sql)
        if params is None:
            args: tuple = ()
        elif isinstance(params, (list, tuple)):
            args = tuple(params)
        else:
            args = (params,)
        s = sql_pg.lstrip().lower()
        if s.startswith(("select", "with", "show", "values")):
            rows = await self._con.fetch(sql_pg, *args)
            return _CursorCompat([dict(r) for r in rows])
        await self._con.execute(sql_pg, *args)
        return _CursorCompat([])

    async def commit(self):
        # asyncpg usa autocommit fora de transações — no-op para compat
        pass

    async def executescript(self, script: str):
        for stmt in _split_sql(script):
            try:
                await self._con.execute(stmt)
            except Exception as e:
                logger.warning(f"executescript: statement falhou — {e} — sql={stmt[:100]}")


@asynccontextmanager
async def get_db():
    """Compat: cede um wrapper que aceita SQL com placeholders '?'."""
    pool = _get_pool()
    async with pool.acquire() as con:
        yield _ConnCompat(con)


# ═══════════════════════════════════════════════════════════════
# Repository — API compatível
# ═══════════════════════════════════════════════════════════════


class Repository:
    """CRUD genérico para uma tabela. API idêntica à versão SQLite."""

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
tools_repo = Repository("tools")
tool_calls_repo = Repository("tool_calls")
traces_repo = Repository("traces")
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
