"""Onda A.2 — Slash invoke direto pra API (skills declarativas).

Diferente de MCP (1 item por tool), API é 1 item por SKILL — porque
api_bindings_parsed compartilham `## Inputs` via Jinja2 e o
execute_declarative orquestra todos como unidade.

Cobertura:
- _extract_template_vars_from_api_bindings (regex Jinja2)
- normalize_api_binding_from_skill (gates declarative + api_bindings,
  precedência Inputs > template_vars, label, api_meta)
- skills-context inclui API bindings junto com MCP
- invoke-binding-direct rota "api": resolve, valida, executa, retorna
- 4xx (binding_id != skill_id, skill não-declarativa, params missing)
- Regressão: SKILL declarativa real (com 2 api_bindings) gera 1 entry
  com fields do ## Inputs, NÃO 2 entries separadas
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ─── SKILL declarativa realista (2 api_bindings, com ## Inputs schema) ───
SKILL_DECLARATIVE_2_BINDINGS = """---
id: urn:skill:erp:subagent:saldo
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Consulta Saldo Cliente

## Purpose
Busca saldo do cliente e notifica time financeiro.

## Activation Criteria
Quando solicitado.

## Inputs
```json
{"type":"object","required":["account_id"],"properties":{"account_id":{"type":"string","description":"ID da conta"},"notify_channel":{"type":"string","default":"#finance"}}}
```

## Workflow
1. Busca saldo.
2. Notifica.

## API Bindings
```yaml
- id: fetch_balance
  connector: ERP
  method: GET
  path: /v1/saldo/{{ inputs.account_id }}
  idempotency_key: "{{ session_id }}"
  output_mapping:
    - from: "$.balance"
      to: "context.customer_balance"
  on_failure: fail

- id: notify_dispatch
  connector: Slack
  method: POST
  path: /api/chat.postMessage
  depends_on: fetch_balance
  body:
    channel: "{{ inputs.notify_channel }}"
    text: "Saldo: {{ context.customer_balance }}"
  body_type: json
  on_failure: continue
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


# ─── SKILL declarativa SEM ## Inputs explícito (testa fallback) ───
SKILL_DECLARATIVE_NO_INPUTS = """---
id: urn:skill:erp:subagent:no-inputs
version: 0.1.0
kind: subagent
owner: e
stability: alpha
execution_mode: declarative
---

# Sem Inputs

## Purpose
Teste fallback.

## Activation Criteria
Quando.

## Inputs
(sem schema)

## Workflow
1. X.

## API Bindings
```yaml
- id: x
  connector: Y
  method: GET
  path: /v1/{{ inputs.thing }}/details
```

## Tool Bindings

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
A única fonte autorizada é o binding **Y** declarado em ## API Bindings.

## Guardrails
- Sem PII.
"""


# ─── SKILL NÃO-declarativa (controle negativo) ───
SKILL_NOT_DECLARATIVE = """---
id: urn:skill:geral:subagent:mcp
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# MCP Skill

## Purpose
MCP.

## Activation Criteria
Quando.

## Inputs
```json
{"type":"object","properties":{"q":{"type":"string"}}}
```

## Workflow
1. X.

## Tool Bindings
- `aaa-bbb-ccc-ddd-eee` (Tool A) — desc.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
A única fonte autorizada é o binding **Tool A** declarado em ## Tool Bindings.

## Guardrails
- Sem PII.
"""


AGENT_ROW = {"id": "agent-A", "name": "Test Agent", "skill_id": "skill-decl"}
SKILL_DECL_ROW = {
    "id": "skill-decl",
    "name": "Saldo Cliente",
    "kind": "subagent",
    "raw_content": SKILL_DECLARATIVE_2_BINDINGS,
}


# ────────────────────────────────────────────────────────────────
# Test client
# ────────────────────────────────────────────────────────────────


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
# Unit: _extract_template_vars_from_api_bindings
# ────────────────────────────────────────────────────────────────


class TestExtractTemplateVars:
    def test_extracts_from_path_and_body(self):
        from app.workspace.binding_schema import _extract_template_vars_from_api_bindings
        bindings = [
            {"id": "x", "path": "/v1/{{ inputs.account_id }}/balance"},
            {"id": "y", "body": {"channel": "{{ inputs.notify_channel }}"}},
        ]
        vars = _extract_template_vars_from_api_bindings(bindings)
        assert set(vars) == {"account_id", "notify_channel"}

    def test_skips_internal_context_vars(self):
        """session_id, context.X — NÃO devem aparecer no form."""
        from app.workspace.binding_schema import _extract_template_vars_from_api_bindings
        bindings = [{
            "id": "x",
            "idempotency_key": "{{ session_id }}",
            "body": {"x": "{{ context.balance }}"},
        }]
        vars = _extract_template_vars_from_api_bindings(bindings)
        # context/session_id são filtrados
        assert "session_id" not in vars
        assert "context" not in vars

    def test_handles_empty_bindings(self):
        from app.workspace.binding_schema import _extract_template_vars_from_api_bindings
        assert _extract_template_vars_from_api_bindings([]) == []
        assert _extract_template_vars_from_api_bindings(None) == []

    def test_dedupes_vars(self):
        from app.workspace.binding_schema import _extract_template_vars_from_api_bindings
        bindings = [
            {"path": "/v1/{{ inputs.x }}"},
            {"body": {"a": "{{ inputs.x }}"}},
        ]
        vars = _extract_template_vars_from_api_bindings(bindings)
        assert vars.count("x") == 1


# ────────────────────────────────────────────────────────────────
# Unit: normalize_api_binding_from_skill
# ────────────────────────────────────────────────────────────────


class TestNormalizeApiBindingFromSkill:
    def test_returns_none_when_not_declarative(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "MCP", "raw_content": SKILL_NOT_DECLARATIVE}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_NOT_DECLARATIVE)
        assert result is None

    def test_uses_inputs_schema_when_present(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "Saldo", "raw_content": SKILL_DECLARATIVE_2_BINDINGS}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_DECLARATIVE_2_BINDINGS)
        assert result is not None
        assert result["binding_kind"] == "api"
        assert result["binding_id"] == "x"
        assert result["binding_label"] == "Saldo"
        assert result["schema_source"] == "skill_inputs"
        names = {f["name"] for f in result["fields"]}
        assert "account_id" in names
        assert "notify_channel" in names

    def test_required_from_inputs_schema_propagates(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "Saldo", "raw_content": SKILL_DECLARATIVE_2_BINDINGS}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_DECLARATIVE_2_BINDINGS)
        acc = next(f for f in result["fields"] if f["name"] == "account_id")
        assert acc["required"] is True
        # notify_channel não está em required → optional
        notify = next(f for f in result["fields"] if f["name"] == "notify_channel")
        assert notify["required"] is False

    def test_falls_back_to_template_vars_when_no_schema(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "y", "name": "X", "raw_content": SKILL_DECLARATIVE_NO_INPUTS}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_DECLARATIVE_NO_INPUTS)
        assert result is not None
        # schema_source agora é template_vars (no inputs schema)
        assert result["schema_source"] == "template_vars"
        names = {f["name"] for f in result["fields"]}
        assert names == {"thing"}

    def test_api_meta_includes_binding_count(self):
        """api_meta.binding_count = quantos api_bindings_parsed a SKILL tem
        — informativo pra UI mostrar "5 chamadas serão feitas".
        """
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "S", "raw_content": SKILL_DECLARATIVE_2_BINDINGS}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_DECLARATIVE_2_BINDINGS)
        assert result["api_meta"]["binding_count"] == 2
        assert set(result["api_meta"]["binding_ids"]) == {"fetch_balance", "notify_dispatch"}


# ────────────────────────────────────────────────────────────────
# Integration: GET /skills-context com API binding
# ────────────────────────────────────────────────────────────────


class TestSkillsContextWithApi:
    def _patch_db(self, monkeypatch, agent=AGENT_ROW, skill=SKILL_DECL_ROW, tools=None):
        if tools is None:
            tools = []

        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return tools

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)

    def test_declarative_skill_yields_one_api_binding_entry(self, app_client, monkeypatch):
        """SKILL declarativa com 2 api_bindings_parsed → 1 entry no contexto
        (não 2). Reflete o modelo: invocação == SKILL inteira."""
        self._patch_db(monkeypatch)
        r = app_client.get(f"/api/v1/workspace/agents/{AGENT_ROW['id']}/skills-context")
        assert r.status_code == 200
        body = r.json()
        skills = body["skills"]
        assert len(skills) == 1
        bindings = skills[0]["bindings"]
        api_bindings = [b for b in bindings if b["binding_kind"] == "api"]
        assert len(api_bindings) == 1
        assert api_bindings[0]["api_meta"]["binding_count"] == 2

    def test_non_declarative_skill_yields_no_api_binding(self, app_client, monkeypatch):
        skill = {"id": "x", "name": "X", "kind": "subagent", "raw_content": SKILL_NOT_DECLARATIVE}
        agent = {**AGENT_ROW, "skill_id": "x"}
        self._patch_db(monkeypatch, agent=agent, skill=skill)
        r = app_client.get(f"/api/v1/workspace/agents/{agent['id']}/skills-context")
        body = r.json()
        bindings = body["skills"][0]["bindings"] if body["skills"] else []
        api_bindings = [b for b in bindings if b["binding_kind"] == "api"]
        assert api_bindings == []


# ────────────────────────────────────────────────────────────────
# Integration: POST /invoke-binding-direct com binding_kind="api"
# ────────────────────────────────────────────────────────────────


class TestInvokeApiBindingDirect:
    def _patch_db(self, monkeypatch, agent=AGENT_ROW, skill=SKILL_DECL_ROW):
        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return []

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)

    def _patch_execute_declarative(self, monkeypatch, return_value=None):
        if return_value is None:
            return_value = {
                "context": {"resposta": "Saldo: 1234.56 BRL"},
                "bindings_executed": [
                    {"binding_id": "fetch_balance", "status": 200, "latency_ms": 120},
                    {"binding_id": "notify_dispatch", "status": 200, "latency_ms": 80},
                ],
                "errors": [],
                "final_state": "completed",
                "output": "Saldo: 1234.56 BRL",
            }
        called = {"args": None}

        # register_interaction (2026-06-02): o route /invoke-binding-direct
        # passa register_interaction=False (é dono da sessão). O mock precisa
        # tolerar o kwarg como a função real faz.
        async def fake_execute(*, agent, skill_parsed, inputs, context, session_id,
                               dry_run, register_interaction=True):
            called["args"] = {
                "agent_id": agent.get("id"),
                "skill_id": getattr(skill_parsed, "frontmatter", None) and skill_parsed.frontmatter.id,
                "inputs": dict(inputs or {}),
                "dry_run": dry_run,
                "session_id": session_id,
                "register_interaction": register_interaction,
            }
            return return_value

        monkeypatch.setattr("app.agents.declarative_engine.execute_declarative", fake_execute)
        return called

    def test_returns_404_when_binding_id_does_not_match_skill_id(
        self, app_client, monkeypatch,
    ):
        self._patch_db(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_DECL_ROW["id"],
            "binding_kind": "api", "binding_id": "outro-id",
            "params": {"account_id": "ACC-1"},
        })
        assert r.status_code == 404

    def test_returns_422_when_skill_not_declarative(self, app_client, monkeypatch):
        skill = {"id": "z", "name": "Z", "kind": "subagent", "raw_content": SKILL_NOT_DECLARATIVE}
        agent = {**AGENT_ROW, "skill_id": "z"}
        self._patch_db(monkeypatch, agent=agent, skill=skill)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": agent["id"], "skill_id": skill["id"],
            "binding_kind": "api", "binding_id": "z",
            "params": {},
        })
        assert r.status_code == 422
        assert "declarativa" in r.json()["detail"].lower()

    def test_returns_422_when_required_input_missing(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        # account_id é required, mandamos só notify_channel
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_DECL_ROW["id"],
            "binding_kind": "api", "binding_id": SKILL_DECL_ROW["id"],
            "params": {"notify_channel": "#geral"},
        })
        assert r.status_code == 422

    def test_calls_execute_declarative_with_inputs(self, app_client, monkeypatch):
        """Confirma que execute_declarative recebe os inputs DIRETAMENTE
        sem passar por LLM. Backend cuida da Jinja2 renderização nas
        api_bindings."""
        self._patch_db(monkeypatch)
        called = self._patch_execute_declarative(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_DECL_ROW["id"],
            "binding_kind": "api", "binding_id": SKILL_DECL_ROW["id"],
            "params": {"account_id": "ACC-1234", "notify_channel": "#financeiro"},
        })
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True
        # Inputs chegaram intactos no engine declarativo
        assert called["args"]["inputs"]["account_id"] == "ACC-1234"
        assert called["args"]["inputs"]["notify_channel"] == "#financeiro"
        # Resposta veio do context.resposta do declarativo
        assert "1234.56" in str(body["result"])
        # 2026-06-02: route é dono da sessão → instrui o engine a NÃO criar
        # uma 2ª interaction "(declarativo)" órfã (que aparecia vazia na sidebar).
        assert called["args"]["register_interaction"] is False

    def test_includes_declarative_metadata_in_response(self, app_client, monkeypatch):
        """UI quer mostrar quantos bindings rodaram + erros — extras vão
        em 'declarative' pra não bagunçar o shape de MCP."""
        self._patch_db(monkeypatch)
        self._patch_execute_declarative(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_DECL_ROW["id"],
            "binding_kind": "api", "binding_id": SKILL_DECL_ROW["id"],
            "params": {"account_id": "ACC"},
        })
        body = r.json()
        assert "declarative" in body
        assert len(body["declarative"]["bindings_executed"]) == 2
        assert body["declarative"]["final_state"] == "completed"

    def test_marks_ok_false_when_any_binding_errored(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute_declarative(monkeypatch, return_value={
            "context": {},
            "bindings_executed": [{"binding_id": "fetch", "status": 500}],
            "errors": ["upstream 500"],
            "final_state": "failed",
            "output": "",
        })
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_DECL_ROW["id"],
            "binding_kind": "api", "binding_id": SKILL_DECL_ROW["id"],
            "params": {"account_id": "ACC"},
        })
        body = r.json()
        assert body["ok"] is False
        assert len(body["declarative"]["errors"]) == 1


# ────────────────────────────────────────────────────────────────
# Regressão arquitetural
# ────────────────────────────────────────────────────────────────


class TestRegressionApiUnitOfInvocation:
    """A.2 modela API como 1 slash item por SKILL (não por binding).
    Razão: api_bindings_parsed compartilham ## Inputs via Jinja2 e
    rodam como unidade. Esses testes provam essa decisão."""

    def test_2_bindings_become_1_canonical_schema(self):
        from app.workspace.binding_schema import normalize_api_binding_from_skill
        skill = {"id": "x", "name": "S", "raw_content": SKILL_DECLARATIVE_2_BINDINGS}
        result = normalize_api_binding_from_skill(skill, skill_md=SKILL_DECLARATIVE_2_BINDINGS)
        # 1 entry, não 2
        assert result is not None
        assert isinstance(result, dict)
        # Fields = inputs (compartilhados), não fields-por-binding
        assert {"account_id", "notify_channel"} == {f["name"] for f in result["fields"]}
        # api_meta diz quantos bindings vão rodar — info defensiva pra UI
        assert result["api_meta"]["binding_count"] == 2
