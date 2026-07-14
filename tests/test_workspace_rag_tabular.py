"""Onda A.3 — Slash invoke direto pra RAG (knowledge sources) + Tabular
(skills declarativas com ## Data Tables).

Generaliza padrão das ondas A.1 (MCP, 1 item por tool) e A.2 (API, 1 item
por SKILL). A.3 traz:
- RAG: 1 item por knowledge_source autorizada na evidence_policy da skill.
  Schema fixo {query, top_n}.
- Tabular: 1 item por SKILL declarativa que tem ## Data Tables. Mesmo
  shape do API binding (kind=tabular quando não há api_bindings).

Cobertura:
- _extract_template_vars generalizado (api_bindings + data_tables)
- normalize_declarative_skill_binding (api/tabular/hybrid kind selection)
- normalize_api_binding_from_skill (back-compat: retorna None pra tabular)
- normalize_rag_binding (schema fixo, metadata)
- skills-context lista RAG sources e bindings declarativos
- invoke-binding-direct rag (mocka Retriever)
- invoke-binding-direct tabular (mesmo helper do api, reusa execute_declarative)
- Gates de governance: evidence_policy.sources, authorized, kb_mode
- Regressão: SKILL com api + tables + rag (3 binding types coexist)
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── SKILL com SÓ Data Tables (declarativa, sem api_bindings) ───
SKILL_TABULAR_ONLY = """---
id: urn:skill:vendas:subagent:relatorio
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Relatório de Vendas

## Purpose
Consulta vendas por região.

## Activation Criteria
Quando solicitado.

## Inputs
```json
{"type":"object","required":["region"],"properties":{"region":{"type":"string","description":"Região"},"max_rows":{"type":"integer","default":50}}}
```

## Workflow
1. Roda query.

## Data Tables
```yaml
tables:
  - id: vendas_q4
    table_ref: urn:table:abc:vendas:1
    inputs:
      - name: region
        if_present: true
    query:
      select: [valor, data, cliente]
      filters:
        - column: region
          operator: EQ
          value: "{{ inputs.region }}"
      order_by: [data DESC]
      limit: 100
```

## Tool Bindings

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
A única fonte autorizada é o binding **vendas_q4** declarado em ## Data Tables.

## Guardrails
- Sem PII.
"""


# ─── SKILL HÍBRIDA: tem ## API Bindings + ## Data Tables ───
SKILL_HYBRID_API_TABULAR = """---
id: urn:skill:erp:subagent:hibrido
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Híbrido API + Tabular

## Purpose
Combo.

## Activation Criteria
Quando.

## Inputs
```json
{"type":"object","required":["account_id"],"properties":{"account_id":{"type":"string"}}}
```

## Workflow
1. Combo.

## API Bindings
```yaml
- id: fetch
  connector: ERP
  method: GET
  path: /v1/saldo/{{ inputs.account_id }}
```

## Data Tables
```yaml
tables:
  - id: historico
    table_ref: urn:table:abc:hist:1
    query:
      select: [data, valor]
      filters:
        - column: account
          operator: EQ
          value: "{{ inputs.account_id }}"
      limit: 30
```

## Tool Bindings

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
A única fonte autorizada é o binding **ERP** declarado em ## API Bindings.

## Guardrails
- Sem PII.
"""


# ─── SKILL com Evidence Policy declarada (autoriza RAG sources) ───
SKILL_WITH_RAG_POLICY = """---
id: urn:skill:rh:subagent:politicas
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Políticas RH

## Purpose
Busca políticas.

## Activation Criteria
Quando.

## Inputs
```json
{"type":"object","properties":{"q":{"type":"string"}}}
```

## Workflow
1. Busca.

## Tool Bindings

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
```yaml
sources:
  - ks-rh-001
  - ks-rh-002
min_relevance: 0.3
cite_sources: true
```

## Guardrails
- Sem PII.
"""


# ─── Knowledge source autorizado ───
RAG_SOURCE_001 = {
    "id": "ks-rh-001",
    "name": "Manual RH",
    "source_type": "pdf_archive",
    "confidentiality_label": "internal",
    "authorized": 1,
    "kb_mode": "hybrid",
}

RAG_SOURCE_002 = {
    "id": "ks-rh-002",
    "name": "FAQ Benefícios",
    "source_type": "html",
    "confidentiality_label": "public",
    "authorized": 1,
    "kb_mode": "text",
}

# Source NÃO autorizada (deve ser filtrada do listing)
RAG_SOURCE_BLOCKED = {
    "id": "ks-secret",
    "name": "Confidencial",
    "source_type": "pdf",
    "confidentiality_label": "restricted",
    "authorized": 0,
    "kb_mode": "hybrid",
}

# Source com kb_mode=tabular (filtrada — RAG textual não aplica)
RAG_SOURCE_TABULAR_ONLY = {
    "id": "ks-tabular",
    "name": "Vendas CSV",
    "source_type": "csv",
    "confidentiality_label": "internal",
    "authorized": 1,
    "kb_mode": "tabular",
}


AGENT_ROW = {"id": "agent-A", "name": "Test Agent", "skill_id": "skill-decl"}


@pytest.fixture
def app_client():
    from app.routes.workspace import router as ws_router
    from app.core.auth import require_user
    app = FastAPI()
    app.include_router(ws_router)

    async def fake_user():
        return {"id": "u1", "email": "test@local"}
    app.dependency_overrides[require_user] = fake_user
    return TestClient(app)


# ────────────────────────────────────────────────────────────────
# Unit: _extract_template_vars (generalizado pra api + data_tables)
# ────────────────────────────────────────────────────────────────


class TestExtractTemplateVarsGeneralized:
    def test_works_on_data_tables(self):
        from app.workspace.binding_schema import _extract_template_vars
        tables = [{
            "id": "vendas",
            "query": {
                "filters": [{"column": "region", "value": "{{ inputs.region }}"}],
            },
        }]
        vars = _extract_template_vars(tables)
        assert "region" in vars

    def test_skips_tables_context_var(self):
        """`{{ tables.X }}` é interno (jsonpath de outro binding) — não
        deve aparecer no form."""
        from app.workspace.binding_schema import _extract_template_vars
        items = [{"x": "{{ tables.vendas }}"}]
        vars = _extract_template_vars(items)
        assert vars == []

    def test_backcompat_alias(self):
        """O alias _extract_template_vars_from_api_bindings continua
        funcionando (callers da A.2 dependem dele)."""
        from app.workspace.binding_schema import _extract_template_vars_from_api_bindings
        items = [{"path": "/v1/{{ inputs.x }}"}]
        assert "x" in _extract_template_vars_from_api_bindings(items)


# ────────────────────────────────────────────────────────────────
# Unit: normalize_declarative_skill_binding
# ────────────────────────────────────────────────────────────────


class TestNormalizeDeclarativeSkillBinding:
    def test_tabular_only_skill_yields_kind_tabular(self):
        from app.workspace.binding_schema import normalize_declarative_skill_binding
        skill = {"id": "x", "name": "Relatório", "raw_content": SKILL_TABULAR_ONLY}
        result = normalize_declarative_skill_binding(skill, skill_md=SKILL_TABULAR_ONLY)
        assert result is not None
        assert result["binding_kind"] == "tabular"
        # Fields vêm de ## Inputs (region required, max_rows opcional)
        names = {f["name"] for f in result["fields"]}
        assert "region" in names
        assert "max_rows" in names
        region = next(f for f in result["fields"] if f["name"] == "region")
        assert region["required"] is True
        # api_meta diferencia api vs tables
        assert result["api_meta"]["binding_count"] == 0
        assert result["api_meta"]["tables_count"] == 1

    def test_hybrid_skill_yields_kind_api(self):
        """SKILL com api_bindings + data_tables → kind=api (prevalece pra UI)."""
        from app.workspace.binding_schema import normalize_declarative_skill_binding
        skill = {"id": "x", "name": "Híbrido", "raw_content": SKILL_HYBRID_API_TABULAR}
        result = normalize_declarative_skill_binding(skill, skill_md=SKILL_HYBRID_API_TABULAR)
        assert result is not None
        assert result["binding_kind"] == "api"
        assert result["api_meta"]["binding_count"] == 1
        assert result["api_meta"]["tables_count"] == 1

    def test_returns_none_for_pure_mcp_skill(self):
        from app.workspace.binding_schema import normalize_declarative_skill_binding
        md = SKILL_TABULAR_ONLY.replace("execution_mode: declarative", "")
        skill = {"id": "x", "name": "X", "raw_content": md}
        result = normalize_declarative_skill_binding(skill, skill_md=md)
        assert result is None


class TestApiBindingBackCompat:
    """normalize_api_binding_from_skill agora delega pro generalizado mas
    mantém semântica antiga: retorna None pra SKILLs tabular-only."""

    def test_api_binding_returns_none_for_tabular_only(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "T", "raw_content": SKILL_TABULAR_ONLY}
        assert normalize_api_binding_from_skill(skill, skill_md=SKILL_TABULAR_ONLY) is None

    def test_api_binding_works_for_hybrid(self):
        """Hybrid (api+tabular) → A.2 enxergava como api. Mantemos."""
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "H", "raw_content": SKILL_HYBRID_API_TABULAR}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_HYBRID_API_TABULAR)
        assert result is not None
        assert result["binding_kind"] == "api"


# ────────────────────────────────────────────────────────────────
# Unit: normalize_rag_binding
# ────────────────────────────────────────────────────────────────


class TestNormalizeRagBinding:
    def test_returns_canonical_with_query_and_top_n(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(RAG_SOURCE_001)
        assert result["binding_kind"] == "rag"
        assert result["binding_id"] == "ks-rh-001"
        assert result["binding_label"] == "Manual RH"
        assert result["schema_source"] == "rag_fixed"
        names = {f["name"] for f in result["fields"]}
        assert names == {"query", "top_n"}

    def test_query_field_is_required_and_multiline(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(RAG_SOURCE_001)
        q = next(f for f in result["fields"] if f["name"] == "query")
        assert q["required"] is True
        assert q["multiline"] is True

    def test_top_n_field_is_optional_with_default_5(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(RAG_SOURCE_001)
        t = next(f for f in result["fields"] if f["name"] == "top_n")
        assert t["required"] is False
        assert t["default"] == 5

    def test_rag_meta_includes_kb_metadata(self):
        from app.workspace.binding_schema import normalize_rag_binding
        result = normalize_rag_binding(RAG_SOURCE_001)
        meta = result["rag_meta"]
        assert meta["source_type"] == "pdf_archive"
        assert meta["confidentiality"] == "internal"
        assert meta["kb_mode"] == "hybrid"
        assert meta["authorized"] is True


# ────────────────────────────────────────────────────────────────
# Integration: skills-context com RAG e Tabular
# ────────────────────────────────────────────────────────────────


class TestSkillsContextWithRagAndTabular:
    def _patch_db(self, monkeypatch, agent, skill, knowledge_sources=None):
        ks = knowledge_sources or {}

        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return []

        async def fake_knowledge_find(kid):
            return ks.get(kid)

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)
        monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_knowledge_find)

    def test_tabular_only_skill_yields_one_binding_kind_tabular(
        self, app_client, monkeypatch,
    ):
        skill = {"id": "x", "name": "Relatório", "kind": "subagent",
                 "raw_content": SKILL_TABULAR_ONLY}
        agent = {**AGENT_ROW, "skill_id": "x"}
        self._patch_db(monkeypatch, agent, skill)
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        body = r.json()
        bindings = body["skills"][0]["bindings"]
        kinds = [b["binding_kind"] for b in bindings]
        assert kinds == ["tabular"]

    def test_skill_with_evidence_policy_lists_rag_sources(
        self, app_client, monkeypatch,
    ):
        skill = {"id": "x", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        agent = {**AGENT_ROW, "skill_id": "x"}
        ks = {
            "ks-rh-001": RAG_SOURCE_001,
            "ks-rh-002": RAG_SOURCE_002,
        }
        self._patch_db(monkeypatch, agent, skill, knowledge_sources=ks)
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        body = r.json()
        bindings = body["skills"][0]["bindings"]
        rag_bindings = [b for b in bindings if b["binding_kind"] == "rag"]
        assert len(rag_bindings) == 2
        labels = {b["binding_label"] for b in rag_bindings}
        assert "Manual RH" in labels

    def test_unauthorized_source_filtered_out(self, app_client, monkeypatch):
        """Source marcada como NÃO autorizada no Registry não aparece no listing."""
        skill_md = SKILL_WITH_RAG_POLICY.replace(
            "  - ks-rh-001\n  - ks-rh-002", "  - ks-secret",
        )
        skill = {"id": "x", "name": "X", "kind": "subagent", "raw_content": skill_md}
        agent = {**AGENT_ROW, "skill_id": "x"}
        ks = {"ks-secret": RAG_SOURCE_BLOCKED}
        self._patch_db(monkeypatch, agent, skill, knowledge_sources=ks)
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        body = r.json()
        bindings = body["skills"][0]["bindings"]
        rag_bindings = [b for b in bindings if b["binding_kind"] == "rag"]
        assert rag_bindings == []

    def test_kb_mode_tabular_source_filtered_out(self, app_client, monkeypatch):
        """kb_mode=tabular não suporta busca textual livre — não aparece em RAG."""
        skill_md = SKILL_WITH_RAG_POLICY.replace(
            "  - ks-rh-001\n  - ks-rh-002", "  - ks-tabular",
        )
        skill = {"id": "x", "name": "X", "kind": "subagent", "raw_content": skill_md}
        agent = {**AGENT_ROW, "skill_id": "x"}
        ks = {"ks-tabular": RAG_SOURCE_TABULAR_ONLY}
        self._patch_db(monkeypatch, agent, skill, knowledge_sources=ks)
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        body = r.json()
        bindings = body["skills"][0]["bindings"]
        rag_bindings = [b for b in bindings if b["binding_kind"] == "rag"]
        assert rag_bindings == []


# ────────────────────────────────────────────────────────────────
# Integration: invoke-binding-direct tabular path (reusa declarative helper)
# ────────────────────────────────────────────────────────────────


class TestInvokeTabularBindingDirect:
    def _patch_db(self, monkeypatch, agent, skill):
        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return []

        async def fake_knowledge_find(kid):
            return None

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)
        monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_knowledge_find)

    def _patch_execute_declarative(self, monkeypatch):
        called = {"args": None}

        # register_interaction (2026-06-02): route é dono da sessão; tolera o
        # kwarg como a função real (execute_declarative) faz.
        async def fake_execute(*, agent, skill_parsed, inputs, context, session_id,
                               dry_run, register_interaction=True):
            called["args"] = {"inputs": dict(inputs or {}), "dry_run": dry_run,
                              "session_id": session_id, "register_interaction": register_interaction}
            return {
                "context": {"resposta": "12 vendas em Sul"},
                "bindings_executed": [
                    {"binding_id": "table:vendas_q4", "status": 200, "latency_ms": 50},
                ],
                "errors": [],
                "final_state": "completed",
                "output": "12 vendas em Sul",
            }

        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_execute)
        return called

    def test_invokes_tabular_only_skill(self, app_client, monkeypatch):
        skill = {"id": "x", "name": "Relatório", "kind": "subagent",
                 "raw_content": SKILL_TABULAR_ONLY}
        agent = {**AGENT_ROW, "skill_id": "x"}
        self._patch_db(monkeypatch, agent, skill)
        called = self._patch_execute_declarative(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"],
            "skill_id": skill["id"],
            "binding_kind": "tabular",
            "binding_id": skill["id"],
            "params": {"region": "Sul", "max_rows": 100},
        })
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True
        # Inputs chegaram ao engine declarativo
        assert called["args"]["inputs"]["region"] == "Sul"
        # Schema source é skill_inputs (## Inputs declarado)
        assert body["schema"]["schema_source"] == "skill_inputs"

    def test_returns_422_when_skill_has_no_tabular(self, app_client, monkeypatch):
        """User pede tabular mas SKILL não tem ## Data Tables."""
        skill_md = """---
id: urn:skill:x:subagent:x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# X

## Purpose
x

## Activation Criteria
x

## Inputs
```json
{"type":"object"}
```

## Workflow
1. x

## Tool Bindings
- `aaa-bbb-ccc-ddd-eee` (Tool) — desc.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- E.

## Evidence Policy
A única fonte autorizada é o binding **Tool** declarado em ## Tool Bindings.

## Guardrails
- Sem PII.
"""
        skill = {"id": "x", "name": "X", "kind": "subagent", "raw_content": skill_md}
        agent = {**AGENT_ROW, "skill_id": "x"}
        self._patch_db(monkeypatch, agent, skill)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "tabular", "binding_id": skill["id"],
            "params": {},
        })
        assert r.status_code == 422


# ────────────────────────────────────────────────────────────────
# Integration: invoke-binding-direct rag path (mocks Retriever)
# ────────────────────────────────────────────────────────────────


@dataclass
class _FakeEvidenceResult:
    evidence_id: str = "ev-1"
    snippet_text: str = "trecho relevante sobre benefícios"
    relevance_score: float = 0.87
    source_name: str = "Manual RH"
    source_id: str = "ks-rh-001"
    confidentiality: str = "internal"


class TestInvokeRagBindingDirect:
    def _patch_db(self, monkeypatch, agent, skill, sources):
        async def fake_agent_find(aid):
            return agent

        async def fake_skill_find(sid):
            return skill

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return []

        async def fake_knowledge_find(kid):
            return sources.get(kid)

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)
        monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_knowledge_find)

    def _patch_retriever(self, monkeypatch, return_value=None):
        if return_value is None:
            return_value = [_FakeEvidenceResult()]
        called = {"args": None}

        async def fake_search(query, skill_evidence_policy=None, top_n=5, allowed_source_ids=None):
            called["args"] = {
                "query": query,
                "top_n": top_n,
                "allowed_source_ids": allowed_source_ids,
            }
            return return_value

        monkeypatch.setattr("app.evidence.runtime.retriever.search", fake_search)
        return called

    def test_returns_404_for_unknown_source(self, app_client, monkeypatch):
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        self._patch_db(monkeypatch, agent, skill, sources={})
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "nonexistent",
            "params": {"query": "x"},
        })
        assert r.status_code == 404

    def test_returns_403_when_source_not_in_evidence_policy(self, app_client, monkeypatch):
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        # ks-other existe no Registry mas NÃO está em evidence_policy.sources
        other_source = {**RAG_SOURCE_001, "id": "ks-other"}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-other": other_source})
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "ks-other",
            "params": {"query": "x"},
        })
        assert r.status_code == 403

    def test_returns_403_when_source_not_authorized(self, app_client, monkeypatch):
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        # Cria policy que cita uma source desautorizada
        skill_md = SKILL_WITH_RAG_POLICY.replace(
            "  - ks-rh-001\n  - ks-rh-002", "  - ks-secret",
        )
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent", "raw_content": skill_md}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-secret": RAG_SOURCE_BLOCKED})
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "ks-secret",
            "params": {"query": "x"},
        })
        assert r.status_code == 403

    def test_returns_422_when_query_missing(self, app_client, monkeypatch):
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-rh-001": RAG_SOURCE_001})
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "ks-rh-001",
            "params": {},
        })
        assert r.status_code == 422

    def test_calls_retriever_with_scoped_source_id(self, app_client, monkeypatch):
        """allowed_source_ids=[binding_id] — slash invoke isola a fonte
        mesmo que outras estejam na evidence_policy."""
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-rh-001": RAG_SOURCE_001})
        called = self._patch_retriever(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "ks-rh-001",
            "params": {"query": "política de férias", "top_n": 3},
        })
        assert r.status_code == 200, r.json()
        body = r.json()
        # Retriever recebeu allowed_source_ids restrita a 1 source
        assert called["args"]["allowed_source_ids"] == ["ks-rh-001"]
        assert called["args"]["query"] == "política de férias"
        assert called["args"]["top_n"] == 3
        # Resposta tem chunks formatados
        assert len(body["result"]["chunks"]) == 1
        assert body["result"]["chunks"][0]["score"] == 0.87

    def test_clamps_top_n_to_safe_range(self, app_client, monkeypatch):
        """top_n=9999 vira 50; top_n=-5 vira 1. Defesa contra payload mau."""
        agent = {**AGENT_ROW, "skill_id": "skill-rh"}
        skill = {"id": "skill-rh", "name": "RH", "kind": "subagent",
                 "raw_content": SKILL_WITH_RAG_POLICY}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-rh-001": RAG_SOURCE_001})
        called = self._patch_retriever(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "rag", "binding_id": "ks-rh-001",
            "params": {"query": "x", "top_n": 9999},
        })
        assert r.status_code == 200
        assert called["args"]["top_n"] == 50


# ────────────────────────────────────────────────────────────────
# UI smoke — workspace.html ainda renderiza tudo
# ────────────────────────────────────────────────────────────────


class TestUiNoChangeNeeded:
    """A.3 não adiciona nada no front. CanonicalFormSchema é genérico —
    UI A.1 já cobre rag (query=multiline, top_n=integer) e tabular
    (mesmos campos do api)."""

    def test_workspace_html_already_handles_all_field_types(self):
        """5 branches no form: enum/multiline/boolean/number/integer/string.
        rag usa multiline + integer. tabular usa o que ## Inputs declarar."""
        from pathlib import Path
        html = Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")
        # Tem branch pra integer (via number input com step=1)
        assert "f.type==='integer'" in html or "f.type==='number'" in html
        # Tem branch pra string multiline (textarea)
        assert "f.multiline" in html


# ────────────────────────────────────────────────────────────────
# Regressão arquitetural — 3 binding types coexist
# ────────────────────────────────────────────────────────────────


SKILL_WITH_ALL_3_TYPES = """---
id: urn:skill:combo:subagent:x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Combo Completo

## Purpose
x

## Activation Criteria
x

## Inputs
```json
{"type":"object","required":["account_id"],"properties":{"account_id":{"type":"string"}}}
```

## Workflow
1. Combo.

## API Bindings
```yaml
- id: fetch
  connector: ERP
  method: GET
  path: /v1/{{ inputs.account_id }}
```

## Data Tables
```yaml
tables:
  - id: hist
    table_ref: urn:table:abc:hist:1
    query:
      select: [v]
      limit: 5
```

## Tool Bindings
- `aaa-bbb-ccc-ddd-eee` (MCP Tool X) — desc.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- E.

## Evidence Policy
```yaml
sources:
  - ks-rh-001
```

## Guardrails
- Sem PII.
"""


class TestRegressionThreeBindingTypes:
    """SKILL com MCP + API + Tabular + RAG simultaneamente. Cada binding
    type vira N items no slash menu:
    - MCP: 1 item por tool no ## Tool Bindings (1 tool aqui)
    - API: 1 item por SKILL (kind=api, prevalece sobre tabular)
    - RAG: 1 item por source autorizada (1 source aqui)
    Total: 3 items (MCP + decl + RAG) — Tabular NÃO vira item separado
    quando há API (kind=api cobre os 2 internamente).
    """

    def _patch_db(self, monkeypatch, agent, skill, sources):
        async def fake_agent_find(aid):
            return agent

        async def fake_skill_find(sid):
            return skill

        # Tool resolvida no Registry pra MCP funcionar
        tool_row = {
            "id": "aaa-bbb-ccc-ddd-eee",
            "name": "MCP Tool X",
            "mcp_server": "https://mcp.test/mcp",
            "operations": "[]",
            "description": "Tool de teste",
            "auth_token": "",
            "auth_config": "{}",
            "auth_requirements": "",
        }

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return [tool_row]

        async def fake_knowledge_find(kid):
            return sources.get(kid)

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)
        monkeypatch.setattr("app.core.database.knowledge_repo.find_by_id", fake_knowledge_find)

    def test_skill_with_all_4_binding_types_yields_3_items(
        self, app_client, monkeypatch,
    ):
        agent = {**AGENT_ROW, "skill_id": "combo"}
        skill = {"id": "combo", "name": "Combo", "kind": "subagent",
                 "raw_content": SKILL_WITH_ALL_3_TYPES}
        self._patch_db(monkeypatch, agent, skill, sources={"ks-rh-001": RAG_SOURCE_001})
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        assert r.status_code == 200
        body = r.json()
        bindings = body["skills"][0]["bindings"]
        kinds = [b["binding_kind"] for b in bindings]
        # Exatamente: mcp, api (engloba api+tabular), rag
        assert sorted(kinds) == ["api", "mcp", "rag"]
        # Confere que o api binding traz info dos 2 grupos via api_meta
        api_b = next(b for b in bindings if b["binding_kind"] == "api")
        assert api_b["api_meta"]["binding_count"] == 1
        assert api_b["api_meta"]["tables_count"] == 1
