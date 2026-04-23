"""Modelo de dados §16 + repositórios."""
import aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "agente_inteligencia.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY, urn TEXT UNIQUE, name TEXT NOT NULL,
    kind TEXT CHECK(kind IN ('orchestrator','router','subagent')) DEFAULT 'subagent',
    domain TEXT, version TEXT DEFAULT '0.1.0',
    stability TEXT CHECK(stability IN ('alpha','beta','stable','deprecated')) DEFAULT 'alpha',
    owner TEXT, purpose TEXT, activation_criteria TEXT,
    inputs_schema TEXT, workflow TEXT, tool_bindings TEXT DEFAULT '[]',
    output_contract TEXT, failure_modes TEXT, delegations TEXT,
    compensation TEXT, guardrails TEXT, budget TEXT DEFAULT '{}',
    examples TEXT DEFAULT '[]', telemetry TEXT, data_dependencies TEXT,
    model_constraints TEXT, evidence_policy TEXT, gold_refs TEXT,
    raw_content TEXT NOT NULL, content_hash TEXT, signed INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    kind TEXT CHECK(kind IN ('aobd','router','subagent')) DEFAULT 'subagent',
    domain TEXT, skill_id TEXT, llm_provider TEXT DEFAULT 'openai',
    model TEXT DEFAULT 'gpt-4o', system_prompt TEXT, config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active', version TEXT DEFAULT '1.0.0',
    temperature REAL DEFAULT 0.7,
    accepts_images INTEGER DEFAULT 0,
    accepts_documents INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (skill_id) REFERENCES skills(id)
);
CREATE TABLE IF NOT EXISTS agent_bindings (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, skill_id TEXT NOT NULL,
    model_serving_ref TEXT, mcp_servers TEXT DEFAULT '[]', config TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (agent_id) REFERENCES agents(id), FOREIGN KEY (skill_id) REFERENCES skills(id)
);
CREATE TABLE IF NOT EXISTS mesh_connections (
    id TEXT PRIMARY KEY, source_agent_id TEXT NOT NULL, target_agent_id TEXT NOT NULL,
    connection_type TEXT DEFAULT 'sequential', config TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (source_agent_id) REFERENCES agents(id), FOREIGN KEY (target_agent_id) REFERENCES agents(id)
);
CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY, trace_id TEXT, span_id TEXT, parent_span_id TEXT,
    origin_agent_id TEXT, origin_skill_urn TEXT,
    target_agent_id TEXT, target_skill_urn TEXT,
    intent TEXT, skill_ref TEXT, context TEXT DEFAULT '{}',
    state_pointer TEXT, budget_remaining TEXT DEFAULT '{}',
    deadline TEXT, status TEXT DEFAULT 'pending', signature TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS journeys (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, domain TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY, title TEXT, started_at TEXT DEFAULT (datetime('now')), ended_at TEXT,
    channel TEXT DEFAULT 'api', agent_id TEXT, customer_hash TEXT,
    journey_id TEXT, state TEXT DEFAULT 'Intake', release_id TEXT,
    metadata TEXT DEFAULT '{}', trace_data TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (agent_id) REFERENCES agents(id)
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, turn_number INTEGER,
    user_text_redacted TEXT, output_text_redacted TEXT,
    interaction_id TEXT NOT NULL, envelope_id TEXT,
    latency_ms REAL DEFAULT 0, tokens_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (interaction_id) REFERENCES interactions(id)
);
CREATE TABLE IF NOT EXISTS knowledge_sources (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
    source_type TEXT, version TEXT, confidentiality_label TEXT DEFAULT 'internal',
    index_version TEXT, last_updated TEXT, authorized INTEGER DEFAULT 1,
    metadata TEXT DEFAULT '{}', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY, snippet_id TEXT, snippet_text TEXT,
    relevance_score REAL, confidentiality_label TEXT,
    knowledge_source_id TEXT, turn_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (knowledge_source_id) REFERENCES knowledge_sources(id),
    FOREIGN KEY (turn_id) REFERENCES turns(id)
);
CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, mcp_server TEXT,
    description TEXT, operations TEXT DEFAULT '[]',
    input_schema TEXT, output_schema TEXT, sla TEXT DEFAULT '{}',
    cost_per_call REAL DEFAULT 0, auth_requirements TEXT,
    auth_token TEXT,
    auth_config TEXT DEFAULT '{}',
    sensitivity TEXT DEFAULT 'internal', requires_trusted_context INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY, tool_name TEXT, mcp_server TEXT,
    input_hash TEXT, output_hash TEXT, input_data TEXT, output_data TEXT,
    latency_ms REAL DEFAULT 0, cost_usd REAL DEFAULT 0,
    interaction_id TEXT, tool_id TEXT, envelope_id TEXT,
    status TEXT DEFAULT 'completed', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY, trace_id TEXT, interaction_id TEXT,
    spans TEXT DEFAULT '[]', duration_ms REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY, name TEXT, environment TEXT DEFAULT 'staging',
    model_config TEXT DEFAULT '{}', prompt_config TEXT DEFAULT '{}',
    index_config TEXT DEFAULT '{}', policy_config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'candidate', baseline_metrics TEXT DEFAULT '{}',
    released_at TEXT, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS gold_cases (
    id TEXT PRIMARY KEY, dataset_version TEXT, case_type TEXT DEFAULT 'normal',
    journey TEXT, channel TEXT, complexity TEXT,
    input_text TEXT NOT NULL, expected_output TEXT NOT NULL,
    expected_state TEXT, metadata TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY, release_id TEXT NOT NULL, gold_version TEXT,
    run_type TEXT DEFAULT 'baseline', total_cases INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    accuracy REAL DEFAULT 0, evidence_coverage REAL DEFAULT 0,
    correct_refusal_rate REAL DEFAULT 0, false_positive_rate REAL DEFAULT 0,
    avg_latency_ms REAL DEFAULT 0, avg_cost_usd REAL DEFAULT 0,
    details TEXT DEFAULT '[]', status TEXT DEFAULT 'running',
    gate_result TEXT, created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (release_id) REFERENCES releases(id)
);
CREATE TABLE IF NOT EXISTS car_entries (
    id TEXT PRIMARY KEY, skill_urn TEXT NOT NULL, domain TEXT,
    activation_keywords TEXT DEFAULT '[]', required_entities TEXT DEFAULT '[]',
    actor_profile TEXT, jurisdiction TEXT, embedding_vector TEXT,
    success_rate REAL DEFAULT 1.0, latency_p95 REAL DEFAULT 0, avg_cost REAL DEFAULT 0,
    status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT,
    details TEXT DEFAULT '{}', trace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS drift_events (
    id TEXT PRIMARY KEY, release_id TEXT, metric_name TEXT,
    baseline_value REAL, current_value REAL, magnitude REAL,
    detection_method TEXT, severity TEXT DEFAULT 'warning',
    resolved INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS platform_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (connector_id) REFERENCES api_connectors(id)
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
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (connector_id) REFERENCES api_connectors(id)
);
"""

async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # ── Migração automática: garante colunas em todas as tabelas ──
        cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in await cur.fetchall()]
        for table in tables:
            col_cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await col_cur.fetchall()}
            if "created_at" not in cols:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN created_at TEXT DEFAULT (datetime('now'))")
                    await db.commit()
                except Exception:
                    pass
        # Migração: title em interactions
        col_cur = await db.execute("PRAGMA table_info(interactions)")
        icols = {r[1] for r in await col_cur.fetchall()}
        if "title" not in icols:
            try:
                await db.execute("ALTER TABLE interactions ADD COLUMN title TEXT")
                await db.commit()
            except Exception:
                pass
        if "trace_data" not in icols:
            try:
                await db.execute("ALTER TABLE interactions ADD COLUMN trace_data TEXT DEFAULT '{}'")
                await db.commit()
            except Exception:
                pass
        # Migração: version em agents
        col_cur2 = await db.execute("PRAGMA table_info(agents)")
        acols = {r[1] for r in await col_cur2.fetchall()}
        if "version" not in acols:
            try:
                await db.execute("ALTER TABLE agents ADD COLUMN version TEXT DEFAULT '1.0.0'")
                await db.commit()
            except Exception:
                pass

        # Migração: mcp_server_type em tools
        col_cur3 = await db.execute("PRAGMA table_info(tools)")
        tcols = {r[1] for r in await col_cur3.fetchall()}
        if "mcp_server_type" not in tcols:
            try:
                await db.execute("ALTER TABLE tools ADD COLUMN mcp_server_type TEXT DEFAULT 'http'")
                await db.commit()
            except Exception:
                pass

        # Migração: auth_token em tools (2026-04-21)
        if "auth_token" not in tcols:
            try:
                await db.execute("ALTER TABLE tools ADD COLUMN auth_token TEXT")
                await db.commit()
            except Exception:
                pass

        # Migração: auth_config em tools (2026-04-21) — JSON para OAuth2/mTLS
        if "auth_config" not in tcols:
            try:
                await db.execute("ALTER TABLE tools ADD COLUMN auth_config TEXT DEFAULT '{}'")
                await db.commit()
            except Exception:
                pass

        # Migração: require_evidence em agents
        if "require_evidence" not in acols:
            try:
                await db.execute("ALTER TABLE agents ADD COLUMN require_evidence INTEGER DEFAULT 1")
                await db.commit()
            except Exception:
                pass

        # Migração: temperature / accepts_images / accepts_documents em agents
        if "temperature" not in acols:
            try:
                await db.execute("ALTER TABLE agents ADD COLUMN temperature REAL DEFAULT 0.7")
                await db.commit()
            except Exception:
                pass
        if "accepts_images" not in acols:
            try:
                await db.execute("ALTER TABLE agents ADD COLUMN accepts_images INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass
        if "accepts_documents" not in acols:
            try:
                await db.execute("ALTER TABLE agents ADD COLUMN accepts_documents INTEGER DEFAULT 0")
                await db.commit()
            except Exception:
                pass

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    try: yield db
    finally: await db.close()

class Repository:
    def __init__(self, table): self.table = table

    async def _order_col(self, db):
        """Detecta coluna de ordenação disponível na tabela."""
        cur = await db.execute(f"PRAGMA table_info({self.table})")
        cols = {r[1] for r in await cur.fetchall()}
        for candidate in ("created_at", "started_at", "id"):
            if candidate in cols:
                return candidate
        return "rowid"

    async def find_all(self, limit=100, offset=0, **filters):
        async with get_db() as db:
            cl, p = [], []
            for k, v in filters.items(): cl.append(f"{k}=?"); p.append(v)
            w = f" WHERE {' AND '.join(cl)}" if cl else ""
            order = await self._order_col(db)
            cur = await db.execute(f"SELECT * FROM {self.table}{w} ORDER BY {order} DESC LIMIT ? OFFSET ?", p+[limit,offset])
            return [dict(r) for r in await cur.fetchall()]

    async def find_by_id(self, id):
        async with get_db() as db:
            cur = await db.execute(f"SELECT * FROM {self.table} WHERE id=?", (id,))
            r = await cur.fetchone(); return dict(r) if r else None

    async def create(self, data):
        async with get_db() as db:
            ks=", ".join(data.keys()); ph=", ".join(["?"]*len(data))
            await db.execute(f"INSERT INTO {self.table} ({ks}) VALUES ({ph})", list(data.values()))
            await db.commit(); return data

    async def update(self, id, data):
        async with get_db() as db:
            s=", ".join(f"{k}=?" for k in data)
            await db.execute(f"UPDATE {self.table} SET {s} WHERE id=?", [*data.values(), id])
            await db.commit(); return await self.find_by_id(id)

    async def delete(self, id):
        async with get_db() as db:
            c=await db.execute(f"DELETE FROM {self.table} WHERE id=?",(id,)); await db.commit(); return c.rowcount>0

    async def count(self, **filters):
        async with get_db() as db:
            cl,p=[],[]
            for k,v in filters.items(): cl.append(f"{k}=?"); p.append(v)
            w=f" WHERE {' AND '.join(cl)}" if cl else ""
            c=await db.execute(f"SELECT COUNT(*) FROM {self.table}{w}",p); return (await c.fetchone())[0]

    async def search(self, query, columns):
        async with get_db() as db:
            order = await self._order_col(db)
            conds=" OR ".join(f"{c} LIKE ?" for c in columns)
            p=[f"%{query}%"]*len(columns)
            cur=await db.execute(f"SELECT * FROM {self.table} WHERE {conds} ORDER BY {order} DESC",p)
            return [dict(r) for r in await cur.fetchall()]

skills_repo=Repository("skills"); agents_repo=Repository("agents")
bindings_repo=Repository("agent_bindings"); mesh_repo=Repository("mesh_connections")
envelopes_repo=Repository("envelopes"); journeys_repo=Repository("journeys")
interactions_repo=Repository("interactions"); turns_repo=Repository("turns")
knowledge_repo=Repository("knowledge_sources"); evidences_repo=Repository("evidences")
tools_repo=Repository("tools"); tool_calls_repo=Repository("tool_calls")
traces_repo=Repository("traces"); releases_repo=Repository("releases")
gold_cases_repo=Repository("gold_cases"); eval_runs_repo=Repository("eval_runs")
car_repo=Repository("car_entries"); audit_repo=Repository("audit_log")
drift_repo=Repository("drift_events")
prompts_repo=Repository("system_prompts")
users_repo=Repository("users")
domains_repo=Repository("domains")
api_connectors_repo = Repository("api_connectors")
api_endpoints_repo = Repository("api_endpoints")
api_call_logs_repo = Repository("api_call_logs")

class SettingsStore:
    """Key-value store para configurações da plataforma."""

    async def get_all(self) -> dict:
        async with get_db() as db:
            cur = await db.execute("SELECT key, value FROM platform_settings")
            rows = await cur.fetchall()
            return {r[0]: r[1] for r in rows}

    async def get(self, key: str, default: str = "") -> str:
        async with get_db() as db:
            cur = await db.execute("SELECT value FROM platform_settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set(self, key: str, value: str):
        async with get_db() as db:
            await db.execute(
                "INSERT INTO platform_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                (key, value),
            )
            await db.commit()

    async def set_many(self, data: dict):
        async with get_db() as db:
            for k, v in data.items():
                await db.execute(
                    "INSERT INTO platform_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                    (k, str(v)),
                )
            await db.commit()

settings_store = SettingsStore()