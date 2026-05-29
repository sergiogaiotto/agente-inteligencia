"""Dry-run de tool MCP no Preview da Skill — Fase 1 (sem chamar servidor).

User pediu (2026-05-29): simular function calling na tela de
preview/validação da skill pra encurtar o loop de feedback.

Cobertura desta fase 1:
- Helpers de parsing (UUIDs em Tool Bindings, split CSV/JSON de operations)
- _build_function_spec reproduz o shape de mcp.runtime:build_openai_tools
- _diagnose incorpora validador estático + checagem de operation no enum
- Endpoint /skills/dry-run-tool: tool resolvida, operation default, override,
  tool inexistente devolve 404
- Caso real do user: SKILL Context7 sem operation= dispara operation.missing
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    from app.routes.skill_dryrun import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


SKILL_GOOD = """---
id: urn:skill:geral:subagent:test
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Test Skill

## Purpose
Teste.

## Activation Criteria
Quando.

## Inputs
```json
{"type":"object"}
```

## Workflow
1. **Chame** a tool `Context 7 MCP Server` com `operation=docs` e `query=<...>`.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro: msg.

## Evidence Policy
A única fonte autorizada é o binding **Context 7 MCP Server** declarado em ## Tool Bindings.

## Guardrails
- Sem PII.
"""


# SKILL real do user (resumida) — Workflow SEM operation=
SKILL_REAL_USER_MISSING_OP = """---
id: urn:skill:geral:subagent:context7-mcp
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Context7 MCP Assistant

## Purpose
Consulta Context7.

## Activation Criteria
Quando.

## Inputs
```json
{"type":"object","properties":{"action":{"type":"string"}}}
```

## Workflow
1. **Valide** o payload.
2. **Chame** a tool `Context 7 MCP Server` com o payload recebido (campo `action`, `subject` e, se aplicável, `content`).
3. **Avalie** a resposta.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro: msg.

## Evidence Policy
A única fonte autorizada é o binding **Context 7 MCP Server** declarado em ## Tool Bindings.

## Guardrails
- Sem PII.
"""


CTX7_TOOL_ROW = {
    "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
    "name": "Context 7 MCP Server",
    "description": "Plataforma Context7 para documentação atualizada",
    "operations": "docs,code,prompt",
}


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────


class TestHelpers:
    def test_extract_tool_id_from_bindings(self):
        from app.routes.skill_dryrun import _extract_tool_id_from_bindings
        md = """## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7) — desc
- `aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee` (Tool 2) — desc
## Outra Seção
"""
        ids = _extract_tool_id_from_bindings(md)
        assert ids == [
            "481c5fa3-36bc-4d05-97ff-d502d93521ff",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ]

    def test_extract_tool_id_returns_empty_when_no_section(self):
        from app.routes.skill_dryrun import _extract_tool_id_from_bindings
        assert _extract_tool_id_from_bindings("# Skill\n\nSem tool bindings.") == []

    def test_split_csv_or_json_csv(self):
        from app.routes.skill_dryrun import _split_csv_or_json
        assert _split_csv_or_json("docs,code,prompt") == ["docs", "code", "prompt"]

    def test_split_csv_or_json_json_list(self):
        from app.routes.skill_dryrun import _split_csv_or_json
        assert _split_csv_or_json('["docs","code"]') == ["docs", "code"]

    def test_split_csv_or_json_empty(self):
        from app.routes.skill_dryrun import _split_csv_or_json
        assert _split_csv_or_json("") == []


# ───────────────────────────────────────────────────────────────
# Function spec construction
# ───────────────────────────────────────────────────────────────


class TestFunctionSpec:
    def test_spec_has_enum_when_operations_declared(self):
        from app.routes.skill_dryrun import _build_function_spec
        spec = _build_function_spec(CTX7_TOOL_ROW)
        op = spec["function"]["parameters"]["properties"]["operation"]
        assert op["enum"] == ["docs", "code", "prompt"]
        # required do schema
        assert set(spec["function"]["parameters"]["required"]) == {"operation", "query"}

    def test_spec_has_no_enum_when_no_operations(self):
        from app.routes.skill_dryrun import _build_function_spec
        tool = {**CTX7_TOOL_ROW, "operations": ""}
        spec = _build_function_spec(tool)
        op = spec["function"]["parameters"]["properties"]["operation"]
        assert "enum" not in op

    def test_function_name_sanitized(self):
        from app.routes.skill_dryrun import _build_function_spec
        tool = {**CTX7_TOOL_ROW, "name": "Tool With Spaces & Special!"}
        spec = _build_function_spec(tool)
        # Espaços e ! removidos; mantém só [a-zA-Z0-9_-]
        assert " " not in spec["function"]["name"]
        assert "!" not in spec["function"]["name"]
        assert "&" not in spec["function"]["name"]


# ───────────────────────────────────────────────────────────────
# Endpoint
# ───────────────────────────────────────────────────────────────


class TestEndpoint:
    def _patch_registry(self, monkeypatch, tool_row=None):
        async def fake_resolve(tool_id):
            if tool_row and tool_row.get("id") == tool_id:
                return tool_row
            return None
        monkeypatch.setattr(
            "app.routes.skill_dryrun._resolve_tool_from_registry",
            fake_resolve,
        )

    def test_endpoint_returns_404_when_tool_not_in_registry(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch, tool_row=None)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": "nonexistent-id",
        })
        assert r.status_code == 404
        assert "Registry" in r.json()["detail"]

    def test_endpoint_returns_400_when_missing_required_fields(self, app_client):
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": "",
            "tool_id": "x",
        })
        assert r.status_code == 400

    def test_endpoint_uses_first_operation_by_default(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch, tool_row=CTX7_TOOL_ROW)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        assert r.status_code == 200
        body = r.json()
        # Operation default = primeira do enum (docs)
        assert body["operation_resolved"] == "docs"
        assert body["payload_that_would_be_sent"]["operation"] == "docs"

    def test_endpoint_respects_operation_override(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch, tool_row=CTX7_TOOL_ROW)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
            "operation_override": "code",
        })
        body = r.json()
        assert body["operation_resolved"] == "code"

    def test_endpoint_flags_operation_override_not_in_enum(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch, tool_row=CTX7_TOOL_ROW)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
            "operation_override": "search",  # NÃO está em docs,code,prompt
        })
        body = r.json()
        assert body["ok"] is False
        rules = {i["rule"] for i in body["issues"]}
        assert "dryrun.operation_not_in_enum" in rules

    def test_endpoint_returns_function_spec(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch, tool_row=CTX7_TOOL_ROW)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        assert "function_spec" in body
        # Function spec é exatamente o que o engine criaria
        op = body["function_spec"]["function"]["parameters"]["properties"]["operation"]
        assert op["enum"] == ["docs", "code", "prompt"]


# ───────────────────────────────────────────────────────────────
# Regressão: SKILL real do user (operation.missing)
# ───────────────────────────────────────────────────────────────


class TestRegressionUserSkillContext7:
    """SKILL real do user (2026-05-29 #5): Workflow não cita operation=
    (passa action/subject/content como payload). Validador estático
    pega operation.missing — dry-run incorpora esse diagnóstico."""

    def _patch_registry(self, monkeypatch):
        async def fake_resolve(tool_id):
            return CTX7_TOOL_ROW if tool_id == CTX7_TOOL_ROW["id"] else None
        monkeypatch.setattr(
            "app.routes.skill_dryrun._resolve_tool_from_registry",
            fake_resolve,
        )

    def test_real_skill_dryrun_fails_with_operation_missing(self, app_client, monkeypatch):
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_REAL_USER_MISSING_OP,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        rules = {i["rule"] for i in body["issues"]}
        # operation.missing detectado pelo validador estático
        assert "operation.missing" in rules

    def test_real_skill_still_shows_payload_for_inspection(self, app_client, monkeypatch):
        """Mesmo com issues, mostra o payload que SERIA enviado — operador
        vê 'olha, mandariam operation=docs (primeira do enum) com query=...'
        e entende o gap."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_REAL_USER_MISSING_OP,
            "tool_id": CTX7_TOOL_ROW["id"],
            "sample_query": "manual python",
        })
        body = r.json()
        # Mesmo com ok=False, payload está montado pra inspeção
        assert body["payload_that_would_be_sent"]["operation"] == "docs"
        assert body["payload_that_would_be_sent"]["query"] == "manual python"
