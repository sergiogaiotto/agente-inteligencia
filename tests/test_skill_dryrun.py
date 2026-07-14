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


class TestUIDynamicParamsAndOperationsParsing:
    """Cobertura do frontend (smoke estático): UI lê function_spec do
    backend pra renderizar formulário dinâmico de N parâmetros + parser
    de operations limpa aspas/colchetes JSON.

    Bug visível no screenshot do user (2026-05-29): operações com JSON
    list `["docs","code","prompt"]` apareciam com aspas literais no
    dropdown. Causa: split(',') sem tratar JSON.
    """

    def test_skill_form_has_parse_operations_helper(self):
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "_parseOperations(raw)" in html
        # Trata JSON list E CSV
        assert "JSON.parse" in html

    def test_skill_form_has_dyn_fields_helper(self):
        """dryRunFieldsFor(tool) deve existir — gera N campos do
        function_spec do backend."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "dryRunFieldsFor(tool)" in html

    def test_skill_form_renders_enum_as_select(self):
        """Campos enum viram <select>, demais input/textarea."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # Branch enum
        assert 'x-if="propMeta.enum && propMeta.enum.length > 0"' in html
        # Branch multiline
        assert "propMeta.multiline" in html

    def test_skill_form_uses_operations_list_instead_of_raw(self):
        """Display 'Operations: docs, code, prompt' deve usar a lista
        parsed — não a string raw (que pode ter aspas/colchetes)."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # Display usa operationsList.join, não o raw
        assert "t.operationsList.join(', ')" in html

    def test_skill_form_initializes_params_dict_per_tool(self):
        """Estado dryRunInputs[tool.id] precisa ser {params: {}} pra
        permitir N campos com nomes dinâmicos. Aceita spacing variations
        após fix do anti-pattern de mutação dentro do getter."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # Aceita "{params: {}}" (original) OU "{ params: {} }" (fix Onda D.1)
        assert "{params: {}}" in html or "{ params: {} }" in html

    def test_runDryRunTool_extracts_canonical_fields_for_backend(self):
        """Backend hoje aceita operation_override + sample_query. O
        runDryRunTool extrai esses 2 do dict params, mantendo back-compat
        enquanto a UI já trabalha com N campos."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "inputs.operation || ''" in html
        # query OU sample_query (compat)
        assert "inputs.query || inputs.sample_query" in html

    def test_skill_form_multiline_heuristic_covers_content_fields(self):
        """Campos com nome content/body/text/payload viram textarea —
        heurística pra payloads grandes."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # Regex de detecção
        assert "content|body|text|payload" in html


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


# ───────────────────────────────────────────────────────────────
# PR #197 (Fase 2): schema custom da SKILL + regra schema.mismatch
# ───────────────────────────────────────────────────────────────


# SKILL realista do user (Context7 MCP Assistant) com schema custom
# {action, subject, content} declarado em ## Inputs. É essa estrutura
# que dispara schema.mismatch na Fase 2.
SKILL_CTX7_CUSTOM_INPUTS = """---
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
Quando solicitado.

## Inputs
```json
{"type":"object","required":["action","subject"],"properties":{"action":{"type":"string","enum":["get_documentation","update_prompt"]},"subject":{"type":"string"},"content":{"type":"string"}}}
```

## Workflow
1. **Valide** o payload.
2. **Chame** a tool `Context 7 MCP Server` com `operation=docs` e `query=<subject>`.
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


class TestExtractInputsSchema:
    """_extract_inputs_schema parseia o JSON Schema da seção ## Inputs."""

    def test_extracts_full_schema_with_properties_and_required(self):
        from app.routes.skill_dryrun import _extract_inputs_schema
        schema = _extract_inputs_schema(SKILL_CTX7_CUSTOM_INPUTS)
        assert schema is not None
        assert schema["type"] == "object"
        assert set(schema["properties"].keys()) == {"action", "subject", "content"}
        assert schema["required"] == ["action", "subject"]
        assert schema["properties"]["action"]["enum"] == [
            "get_documentation", "update_prompt"
        ]

    def test_returns_none_when_no_inputs_section(self):
        from app.routes.skill_dryrun import _extract_inputs_schema
        md = "# Skill\n\n## Purpose\nteste\n"
        assert _extract_inputs_schema(md) is None

    def test_returns_none_when_no_fenced_block(self):
        """## Inputs sem ```json``` deve retornar None."""
        from app.routes.skill_dryrun import _extract_inputs_schema
        md = "# X\n\n## Inputs\nSem fenced block.\n\n## Outra\n"
        assert _extract_inputs_schema(md) is None

    def test_returns_none_when_malformed_json(self):
        from app.routes.skill_dryrun import _extract_inputs_schema
        md = """# X

## Inputs
```json
{not valid json}
```

## Outra
"""
        assert _extract_inputs_schema(md) is None

    def test_returns_none_when_schema_has_no_properties(self):
        """Schema sem properties não vira function spec útil."""
        from app.routes.skill_dryrun import _extract_inputs_schema
        md = """# X

## Inputs
```json
{"type":"object"}
```

## Outra
"""
        assert _extract_inputs_schema(md) is None

    def test_returns_none_when_empty(self):
        from app.routes.skill_dryrun import _extract_inputs_schema
        assert _extract_inputs_schema("") is None


class TestBuildFunctionSpecFromSkillInputs:
    """_build_function_spec_from_skill_inputs reflete schema declarado."""

    def test_preserves_properties_from_schema(self):
        from app.routes.skill_dryrun import _build_function_spec_from_skill_inputs
        inputs = {
            "type": "object",
            "required": ["action", "subject"],
            "properties": {
                "action": {"type": "string", "enum": ["get_doc", "upd"]},
                "subject": {"type": "string"},
                "content": {"type": "string"},
            },
        }
        spec = _build_function_spec_from_skill_inputs(CTX7_TOOL_ROW, inputs)
        params = spec["function"]["parameters"]
        assert set(params["properties"].keys()) == {"action", "subject", "content"}
        assert params["required"] == ["action", "subject"]
        # Function name sanitizado
        assert " " not in spec["function"]["name"]
        # Description sinaliza origem
        assert "SKILL-declared" in spec["function"]["description"]

    def test_uses_object_type_when_schema_missing_type(self):
        from app.routes.skill_dryrun import _build_function_spec_from_skill_inputs
        inputs = {"properties": {"x": {"type": "string"}}}
        spec = _build_function_spec_from_skill_inputs(CTX7_TOOL_ROW, inputs)
        assert spec["function"]["parameters"]["type"] == "object"


class TestSchemaMismatchDetection:
    """_schemas_have_field_mismatch compara nomes de fields entre os 2 specs."""

    def test_returns_none_when_skill_spec_absent(self):
        from app.routes.skill_dryrun import _schemas_have_field_mismatch
        engine = {"function": {"parameters": {"properties": {"a": {}}}}}
        assert _schemas_have_field_mismatch(engine, None) is None

    def test_detects_field_present_only_in_skill(self):
        from app.routes.skill_dryrun import _schemas_have_field_mismatch
        skill = {"function": {"parameters": {"properties": {
            "action": {}, "subject": {}, "content": {},
        }}}}
        engine = {"function": {"parameters": {"properties": {
            "operation": {}, "query": {},
        }}}}
        skill_only, engine_only = _schemas_have_field_mismatch(engine, skill)
        assert skill_only == ["action", "content", "subject"]
        assert engine_only == ["operation", "query"]

    def test_returns_empty_lists_when_schemas_match(self):
        from app.routes.skill_dryrun import _schemas_have_field_mismatch
        spec = {"function": {"parameters": {"properties": {
            "operation": {}, "query": {},
        }}}}
        skill_only, engine_only = _schemas_have_field_mismatch(spec, spec)
        assert skill_only == []
        assert engine_only == []


class TestEndpointPhase2:
    """Endpoint retorna function_spec_skill_declared + dispara
    schema.mismatch quando há divergência."""

    def _patch_registry(self, monkeypatch):
        async def fake_resolve(tool_id):
            return CTX7_TOOL_ROW if tool_id == CTX7_TOOL_ROW["id"] else None
        monkeypatch.setattr(
            "app.routes.skill_dryrun._resolve_tool_from_registry",
            fake_resolve,
        )

    def test_returns_skill_declared_spec_when_inputs_has_schema(
        self, app_client, monkeypatch,
    ):
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_CTX7_CUSTOM_INPUTS,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        assert body["function_spec_skill_declared"] is not None
        skill_params = body["function_spec_skill_declared"]["function"]["parameters"]
        assert set(skill_params["properties"].keys()) == {
            "action", "subject", "content",
        }

    def test_skill_declared_spec_is_none_when_no_inputs_schema(
        self, app_client, monkeypatch,
    ):
        """SKILL_GOOD tem ## Inputs ```json {"type":"object"} ``` (sem properties)
        — Phase 2 deve retornar None."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        assert body["function_spec_skill_declared"] is None

    def test_schema_mismatch_no_longer_fires_after_onda_b(
        self, app_client, monkeypatch,
    ):
        """Onda B: engine agora respeita ## Inputs da SKILL — function_spec
        do engine vira EQUAL ao function_spec_skill_declared. Mismatch só
        dispararia em situação anômala (drift de implementação). Antes da
        Onda B este teste assertava o mismatch existir (causa raiz dos
        bugs Context7); agora asserta que foi RESOLVIDO."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_CTX7_CUSTOM_INPUTS,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        rules = [i["rule"] for i in body["issues"]]
        # Mismatch NÃO está mais nas issues — engine respeita a SKILL
        assert "schema.mismatch" not in rules
        # function_spec do engine agora mostra os fields da SKILL
        engine_props = body["function_spec"]["function"]["parameters"]["properties"]
        assert set(engine_props.keys()) == {"action", "subject", "content"}

    def test_no_schema_mismatch_when_skill_has_no_inputs_schema(
        self, app_client, monkeypatch,
    ):
        """SKILL sem schema parseável em ## Inputs não dispara mismatch."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        rules = [i["rule"] for i in body["issues"]]
        assert "schema.mismatch" not in rules

    def test_extra_params_reflected_in_payload(
        self, app_client, monkeypatch,
    ):
        """Quando user envia extra_params com schema custom, payload
        simulado reflete os valores — operador vê o JSON que SERIA
        enviado se engine respeitasse o schema da SKILL."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_CTX7_CUSTOM_INPUTS,
            "tool_id": CTX7_TOOL_ROW["id"],
            "extra_params": {
                "action": "get_documentation",
                "subject": "python asyncio",
                "content": "",
            },
        })
        body = r.json()
        payload = body["payload_that_would_be_sent"]
        assert payload["action"] == "get_documentation"
        assert payload["subject"] == "python asyncio"
        # operation ainda vai pro payload pra observabilidade
        assert "operation" in payload

    def test_backcompat_payload_when_no_extra_params(
        self, app_client, monkeypatch,
    ):
        """Sem extra_params, payload é {operation, query} (Fase 1)."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_GOOD,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        assert set(body["payload_that_would_be_sent"].keys()) == {"operation", "query"}


class TestUIPhase2:
    """UI smoke checks: prioriza schema da SKILL no form + envia extra_params."""

    def test_frontend_prioritizes_skill_declared_spec(self):
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # dryRunFieldsFor lê function_spec_skill_declared antes do function_spec
        assert "function_spec_skill_declared" in html
        assert "skillSpec || engineSpec" in html

    def test_frontend_sends_extra_params(self):
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # runDryRunTool monta extra_params do dict de params do tool
        assert "extra_params:" in html

    def test_frontend_shows_dual_schema_side_by_side(self):
        """Bloco com SKILL spec + engine spec lado a lado quando há mismatch."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "Schema declarado em ## Inputs" in html
        assert "Spec que o LLM REALMENTE vê hoje" in html

    def test_frontend_shows_badge_when_skill_drives_form(self):
        """Badge SKILL avisa o operador qual spec dirige o form."""
        from pathlib import Path
        html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "schema custom de" in html


class TestRegressionContext7SchemaMismatch:
    """Regressão completa: SKILL Context7 MCP Assistant do user dispara
    schema.mismatch — provando que dry-run agora ENCONTRA o gap
    arquitetural que causou os bugs Context7 #1-#5."""

    def _patch_registry(self, monkeypatch):
        async def fake_resolve(tool_id):
            return CTX7_TOOL_ROW if tool_id == CTX7_TOOL_ROW["id"] else None
        monkeypatch.setattr(
            "app.routes.skill_dryrun._resolve_tool_from_registry",
            fake_resolve,
        )

    def test_user_skill_ctx7_v6_now_surfaces_schema_mismatch(
        self, app_client, monkeypatch,
    ):
        """SKILL v6 do user (Context7 MCP Assistant) tem ## Inputs com
        {action, subject, content}. Sem Fase 2, dry-run só sinalizava
        operation.* mas o operador não via que o ROOT cause era o
        engine forçar {operation, query} no shape errado."""
        self._patch_registry(monkeypatch)
        r = app_client.post("/api/v1/skills/dry-run-tool", json={
            "skill_md": SKILL_CTX7_CUSTOM_INPUTS,
            "tool_id": CTX7_TOOL_ROW["id"],
        })
        body = r.json()
        # Onda B: function_spec_skill_declared continua mostrando schema da SKILL...
        assert body["function_spec_skill_declared"] is not None
        # ...E function_spec do engine agora ALINHA com ele (Onda B respeita ## Inputs)
        engine_props = body["function_spec"]["function"]["parameters"]["properties"]
        assert set(engine_props.keys()) == {"action", "subject", "content"}
        # schema.mismatch foi resolvido — engine não força mais {operation, query}
        rules = [i["rule"] for i in body["issues"]]
        assert "schema.mismatch" not in rules
