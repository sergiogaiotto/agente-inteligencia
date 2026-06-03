"""Onda A.1 — Endpoints /workspace/agents/{id}/skills-context e
/workspace/invoke-binding-direct.

Cobertura:
- skills-context: lista skills do agente + bindings + CanonicalFormSchema
- invoke-binding-direct: chama execute_tool_call sem LLM, mockando MCP server
- Erros 4xx (agent inexistente, skill inexistente, binding ID errado,
  binding_kind não-MCP, validação de params)
- Smoke estático do workspace.html (slash UI 2-níveis, form inline, fetch
  de bindings, métodos esperados)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


SKILL_CTX7 = """---
id: urn:skill:geral:subagent:context7
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Context7 MCP Assistant

## Purpose
Consulta Context7.

## Inputs
```json
{"type":"object","required":["action","subject"],"properties":{"action":{"type":"string","enum":["get_documentation","update_prompt"]},"subject":{"type":"string"},"content":{"type":"string"}}}
```

## Workflow
1. **Chame** a tool.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — Plataforma.

## Output Contract
```json
{"type":"object"}
```

## Failure Modes
- Erro.

## Evidence Policy
A única fonte autorizada é o binding **Context 7 MCP Server** declarado em ## Tool Bindings.

## Guardrails
- Sem PII.
"""

AGENT_ROW = {
    "id": "agent-123",
    "name": "Agente Test",
    "skill_id": "skill-456",
}

SKILL_ROW = {
    "id": "skill-456",
    "name": "Context7 Assistant",
    "kind": "subagent",
    "raw_content": SKILL_CTX7,
}

CTX7_TOOL_ROW = {
    "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
    "name": "Context 7 MCP Server",
    "mcp_server": "https://mcp.context7.com/mcp",
    "operations": '["docs","code","prompt"]',
    "description": "Plataforma Context7",
    "auth_token": "",
    "auth_config": "{}",
    "auth_requirements": "",
}


@pytest.fixture
def app_client():
    """TestClient com router de workspace + auth bypass.

    Override de Depends(require_user) pra retornar user dummy sem mexer em
    cookie/session — testa só a lógica da rota.
    """
    from app.routes.workspace import router as ws_router
    from app.core.auth import require_user
    app = FastAPI()
    app.include_router(ws_router)

    async def fake_user():
        return {"id": "u1", "email": "test@local"}
    app.dependency_overrides[require_user] = fake_user
    return TestClient(app)


# ────────────────────────────────────────────────────────────────
# GET /workspace/agents/{id}/skills-context
# ────────────────────────────────────────────────────────────────


class TestSkillsContext:
    def _patch_db(self, monkeypatch, agent=AGENT_ROW, skill=SKILL_ROW, tools=None):
        if tools is None:
            tools = [CTX7_TOOL_ROW]

        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return tools

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)

    def test_returns_404_for_unknown_agent(self, app_client, monkeypatch):
        self._patch_db(monkeypatch, agent=None)
        r = app_client.get("/api/v1/workspace/agents/missing/skills-context")
        assert r.status_code == 404

    def test_returns_skills_and_bindings(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        r = app_client.get(f"/api/v1/workspace/agents/{AGENT_ROW['id']}/skills-context")
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == AGENT_ROW["id"]
        assert len(body["skills"]) == 1
        sk = body["skills"][0]
        assert sk["skill_id"] == SKILL_ROW["id"]
        assert len(sk["bindings"]) == 1
        b = sk["bindings"][0]
        assert b["binding_kind"] == "mcp"
        assert b["binding_label"] == "Context 7 MCP Server"
        # Schema da SKILL ## Inputs é o usado
        assert b["schema_source"] == "skill_inputs"
        field_names = {f["name"] for f in b["fields"]}
        assert field_names == {"action", "subject", "content"}

    def test_handles_agent_without_skill(self, app_client, monkeypatch):
        agent_no_skill = {**AGENT_ROW, "skill_id": None}
        self._patch_db(monkeypatch, agent=agent_no_skill, skill=None)
        r = app_client.get(f"/api/v1/workspace/agents/{AGENT_ROW['id']}/skills-context")
        assert r.status_code == 200
        assert r.json()["skills"] == []


# ────────────────────────────────────────────────────────────────
# POST /workspace/invoke-binding-direct
# ────────────────────────────────────────────────────────────────


class TestInvokeBindingDirect:
    def _patch_db(self, monkeypatch, agent=AGENT_ROW, skill=SKILL_ROW, tools=None):
        if tools is None:
            tools = [CTX7_TOOL_ROW]

        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return tools

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)

    def _patch_execute(self, monkeypatch, return_value='{"result": "ok"}'):
        called = {"args": None}

        async def fake_execute(tool_name, arguments, mcp_tools, timeout=60):
            called["args"] = {"tool_name": tool_name, "arguments": arguments, "timeout": timeout}
            return return_value

        monkeypatch.setattr("app.mcp.runtime.execute_tool_call", fake_execute)
        return called

    def test_returns_404_for_unknown_agent(self, app_client, monkeypatch):
        self._patch_db(monkeypatch, agent=None)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": "x", "skill_id": "y", "binding_kind": "mcp",
            "binding_id": "z", "params": {},
        })
        assert r.status_code == 404

    def test_returns_404_for_unknown_skill(self, app_client, monkeypatch):
        self._patch_db(monkeypatch, skill=None)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": "missing",
            "binding_kind": "mcp", "binding_id": "z", "params": {},
        })
        assert r.status_code == 404

    def test_returns_422_for_invalid_binding_kind(self, app_client, monkeypatch):
        """A.1+A.2+A.3 cobrem todos os 4 kinds (mcp/api/tabular/rag).
        Pydantic regex rejeita anything else com 422."""
        self._patch_db(monkeypatch)
        for invalid in ("invalid", "MCP", "rest"):
            r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
                "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
                "binding_kind": invalid, "binding_id": "z", "params": {},
            })
            assert r.status_code == 422, f"{invalid} should be rejected by Pydantic"

    def test_returns_404_for_unknown_binding_id(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": "wrong-id", "params": {},
        })
        assert r.status_code == 404

    def test_returns_422_when_required_param_missing(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        # SKILL Context7 exige action + subject; mandamos só action
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "docs",
            "params": {"action": "get_documentation"},  # falta subject
        })
        assert r.status_code == 422

    def test_invokes_execute_tool_call_with_full_params(self, app_client, monkeypatch):
        """User manda action/subject/content — todos vão pro arguments
        do execute_tool_call. Engine MCP runtime cuida do mapping pra
        inputSchema real do servidor (sem LLM)."""
        self._patch_db(monkeypatch)
        called = self._patch_execute(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": {
                "action": "get_documentation",
                "subject": "python asyncio",
                "content": "manual completo",
            },
        })
        assert r.status_code == 200, r.json()
        body = r.json()
        assert body["ok"] is True
        # Confere que execute_tool_call recebeu os params completos
        args = called["args"]["arguments"]
        assert args["operation"] == "get_documentation"
        assert args["subject"] == "python asyncio"
        assert args["content"] == "manual completo"
        # Payload_sent reflete os mesmos params (rastreabilidade)
        assert body["payload_sent"]["subject"] == "python asyncio"

    def test_uses_first_string_field_as_query_fallback(self, app_client, monkeypatch):
        """Quando user não preenche field "query" explícito, backend pega
        primeiro string preenchido pra back-compat com servidores que
        esperam 'query' obrigatório."""
        self._patch_db(monkeypatch)
        called = self._patch_execute(monkeypatch)
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": {
                "action": "get_documentation",
                "subject": "asyncio",
            },
        })
        assert r.status_code == 200
        args = called["args"]["arguments"]
        # 'query' veio do action ou subject (primeiro string)
        assert "query" in args

    def test_parses_json_result_when_possible(self, app_client, monkeypatch):
        """Se execute_tool_call devolve JSON serializado, endpoint parseia
        pra UI renderizar bonito."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch, return_value='{"items": [1, 2, 3]}')
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": {"action": "get_documentation", "subject": "x"},
        })
        body = r.json()
        assert isinstance(body["result"], dict)
        assert body["result"]["items"] == [1, 2, 3]

    def test_marks_ok_false_when_tool_returns_error(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch, return_value='{"error": "tool falhou"}')
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": {"action": "get_documentation", "subject": "x"},
        })
        body = r.json()
        assert body["ok"] is False

    def test_keeps_raw_string_when_not_json(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch, return_value="Plain text result")
        r = app_client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": {"action": "get_documentation", "subject": "x"},
        })
        body = r.json()
        assert body["result"] == "Plain text result"
        assert body["result_raw"] == "Plain text result"


# ────────────────────────────────────────────────────────────────
# UI smoke — workspace.html tem os hooks necessários
# ────────────────────────────────────────────────────────────────


class TestWorkspaceUISmoke:
    def _html(self):
        from pathlib import Path
        return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")

    def test_has_bindings_state(self):
        html = self._html()
        assert "bindingsContext:" in html
        assert "activeBindingForm:" in html
        assert "bindingFormParams:" in html

    def test_has_fetch_bindings_context(self):
        html = self._html()
        assert "_fetchBindingsContext" in html
        assert "/api/v1/workspace/agents/" in html
        assert "/skills-context" in html

    def test_has_invoke_binding_direct(self):
        html = self._html()
        assert "invokeBindingDirect" in html
        assert "/api/v1/workspace/invoke-binding-direct" in html

    def test_slash_menu_includes_dynamic_bindings(self):
        """filteredSlashCmds expande com bindings do skills-context."""
        html = self._html()
        # Loop sobre skills + bindings injeta items com _kind='binding'
        assert "_kind: 'binding'" in html
        assert "binding_kind" in html

    def test_inline_form_renders_all_field_types(self):
        """Form template tem todos os branches: enum, multiline, boolean,
        number/integer, string."""
        html = self._html()
        # Branch enum
        assert "f.type==='enum'" in html
        # Branch multiline (textarea)
        assert "f.multiline" in html
        # Branch boolean (checkbox)
        assert "f.type==='boolean'" in html
        # Branch number/integer
        assert "f.type==='number'" in html or "f.type==='integer'" in html

    def test_form_has_invoke_and_cancel_buttons(self):
        html = self._html()
        assert "Invocar" in html
        assert "Cancelar" in html
        assert "cancelBindingForm" in html

    def test_chat_renders_binding_invoke_history(self):
        """Mensagem do user marca _isBindingInvoke; assistant response
        marca _bindingInvokeResult com result do invoke direto."""
        html = self._html()
        assert "_isBindingInvoke" in html
        assert "_bindingInvokeResult" in html

    def test_has_translate_result_toggle(self):
        """Toggle opt-in 'Traduzir resultado' existe, tem state Alpine próprio
        e é enviado no POST (translate_result). Escopado a MCP no template."""
        html = self._html()
        assert "bindingTranslateResult" in html
        assert "translate_result: this.bindingTranslateResult" in html
        assert "Traduzir resultado" in html
        # Visível só pra binding MCP
        assert "binding_kind === 'mcp'" in html


# ────────────────────────────────────────────────────────────────
# POST /workspace/invoke-binding-direct — Localização opt-in (translate_result)
# ────────────────────────────────────────────────────────────────


class TestInvokeBindingLocalization:
    """translate_result=True roda UM passe de LLM pós-tool traduzindo o
    resultado pro idioma de resposta resolvido (agent.response_language >
    settings.default_response_language > pt-BR).

    Invariantes:
    - OFF por padrão: get_provider NUNCA é chamado; resultado volta cru.
    - ON: resultado textual é traduzido; URLs/estrutura preservadas.
    - Best-effort: falha do provider devolve o original, ok permanece True.
    - is_error (tool falhou) NÃO dispara tradução.
    """

    def _patch_db(self, monkeypatch, agent=AGENT_ROW, skill=SKILL_ROW, tools=None):
        if tools is None:
            tools = [CTX7_TOOL_ROW]

        async def fake_agent_find(aid):
            return agent if (agent and agent["id"] == aid) else None

        async def fake_skill_find(sid):
            return skill if (skill and skill["id"] == sid) else None

        async def fake_tools_find_all(limit=200, offset=0, **filters):
            return tools

        monkeypatch.setattr("app.core.database.agents_repo.find_by_id", fake_agent_find)
        monkeypatch.setattr("app.core.database.skills_repo.find_by_id", fake_skill_find)
        monkeypatch.setattr("app.core.database.tools_repo.find_all", fake_tools_find_all)

    def _patch_execute(self, monkeypatch,
                       return_value='{"title": "Best Practices", "url": "https://learn.openapis.org"}'):
        async def fake_execute(tool_name, arguments, mcp_tools, timeout=60):
            return return_value
        monkeypatch.setattr("app.mcp.runtime.execute_tool_call", fake_execute)

    def _patch_provider(self, monkeypatch,
                        content='{"title": "Melhores Práticas", "url": "https://learn.openapis.org"}',
                        raise_exc=None):
        """Mocka app.core.llm_providers.get_provider (importado dentro do helper
        em runtime → monkeypatch no módulo pega). Captura chamadas/mensagens."""
        calls = {"count": 0, "name": None, "kwargs": None, "messages": None}

        class FakeProvider:
            def __init__(self, **kwargs):
                calls["kwargs"] = kwargs

            async def generate(self, messages, **kwargs):
                calls["messages"] = messages
                if raise_exc is not None:
                    raise raise_exc
                return {"content": content}

        def fake_get_provider(name="azure", **kwargs):
            calls["count"] += 1
            calls["name"] = name
            return FakeProvider(**kwargs)

        monkeypatch.setattr("app.core.llm_providers.get_provider", fake_get_provider)
        return calls

    def _invoke(self, app_client, translate_result, params=None):
        payload = {
            "agent_id": AGENT_ROW["id"], "skill_id": SKILL_ROW["id"],
            "binding_kind": "mcp", "binding_id": CTX7_TOOL_ROW["id"],
            "operation": "get_documentation",
            "params": params or {"action": "get_documentation", "subject": "openapi"},
        }
        if translate_result is not None:
            payload["translate_result"] = translate_result
        return app_client.post("/api/v1/workspace/invoke-binding-direct", json=payload)

    def test_off_by_default_skips_llm(self, app_client, monkeypatch):
        """Sem translate_result no payload (default False): get_provider NÃO é
        chamado e o resultado volta cru (idioma da fonte)."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        calls = self._patch_provider(monkeypatch)
        body = self._invoke(app_client, translate_result=None).json()
        assert calls["count"] == 0, "LLM não deve rodar quando OFF"
        assert body["result"]["title"] == "Best Practices"  # cru
        assert body["localized"] is False

    def test_explicit_false_skips_llm(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        calls = self._patch_provider(monkeypatch)
        body = self._invoke(app_client, translate_result=False).json()
        assert calls["count"] == 0
        assert body["localized"] is False

    def test_on_translates_json_result(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        calls = self._patch_provider(monkeypatch)
        r = self._invoke(app_client, translate_result=True)
        assert r.status_code == 200, r.json()
        body = r.json()
        assert calls["count"] == 1
        assert body["localized"] is True
        assert body["result"]["title"] == "Melhores Práticas"  # traduzido
        assert body["result"]["url"] == "https://learn.openapis.org"  # preservado

    def test_on_adds_localize_entry_to_execution_log(self, app_client, monkeypatch):
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        self._patch_provider(monkeypatch)
        body = self._invoke(app_client, translate_result=True).json()
        log = body["trace"]["trace"]["execution_log"]
        loc = [e for e in log if e.get("cat") == "localize"]
        assert len(loc) == 1
        assert "Tradução" in loc[0]["title"]
        assert "pt-BR" in loc[0]["title"]  # default settings (pt-BR)
        assert loc[0]["level"] == "info"

    def test_provider_error_falls_back_to_original(self, app_client, monkeypatch):
        """Best-effort: LLM falha → resultado original, ok=True, localized=False,
        e o log marca warning (a invocação NÃO é derrubada)."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        self._patch_provider(monkeypatch, raise_exc=RuntimeError("provider down"))
        body = self._invoke(app_client, translate_result=True).json()
        assert body["ok"] is True
        assert body["localized"] is False
        assert body["result"]["title"] == "Best Practices"  # original preservado
        log = body["trace"]["trace"]["execution_log"]
        loc = [e for e in log if e.get("cat") == "localize"]
        assert len(loc) == 1 and loc[0]["level"] == "warning"

    def test_invalid_json_translation_falls_back(self, app_client, monkeypatch):
        """LLM devolve algo que não é JSON válido → preserva o original (não
        arrisca quebrar o render estruturado)."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch)
        self._patch_provider(monkeypatch, content="isto não é json")
        body = self._invoke(app_client, translate_result=True).json()
        assert body["localized"] is False
        assert body["result"]["title"] == "Best Practices"

    def test_respects_agent_response_language_cascade(self, app_client, monkeypatch):
        """agent.response_language tem precedência sobre settings — o passe de
        tradução mira es-ES quando o agente declara es-ES."""
        agent_es = {**AGENT_ROW, "response_language": "es-ES"}
        self._patch_db(monkeypatch, agent=agent_es)
        self._patch_execute(monkeypatch)
        calls = self._patch_provider(monkeypatch)
        body = self._invoke(app_client, translate_result=True).json()
        sys_prompt = calls["messages"][0]["content"]
        assert "es-ES" in sys_prompt
        diags = body["trace"]["trace"]["diagnostics"]
        assert any("es-ES" in (d.get("text") or "") for d in diags)

    def test_no_translation_when_tool_errors(self, app_client, monkeypatch):
        """Tool retornou erro → não há conteúdo útil pra traduzir; LLM não roda."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch, return_value='{"error": "tool falhou"}')
        calls = self._patch_provider(monkeypatch)
        body = self._invoke(app_client, translate_result=True).json()
        assert body["ok"] is False
        assert calls["count"] == 0
        assert body["localized"] is False

    def test_translates_plain_string_result(self, app_client, monkeypatch):
        """Resultado string pura também é traduzido (não-JSON)."""
        self._patch_db(monkeypatch)
        self._patch_execute(monkeypatch, return_value="Best practices for OpenAPI")
        self._patch_provider(monkeypatch, content="Melhores práticas para OpenAPI")
        body = self._invoke(app_client, translate_result=True).json()
        assert body["localized"] is True
        assert body["result"] == "Melhores práticas para OpenAPI"
