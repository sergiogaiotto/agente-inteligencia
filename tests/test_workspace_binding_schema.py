"""Onda A.1 — Normalizador de schemas de bindings.

Cobertura:
- _extract_inputs_schema (cópia local do helper canônico)
- _fields_from_json_schema (mapeia JSON Schema → fields canônicos)
- _fields_from_operations (fallback {operation, query})
- normalize_mcp_binding (orquestra precedência: SKILL > tool.inputSchema > legacy)
- validate_params_against_schema (required + enum)
- Regressão: SKILL Context7 v6 do user (action, subject, content) gera
  schema canônico com 3 fields — não comprimido em {operation, query}.
"""
from __future__ import annotations



# ────────────────────────────────────────────────────────────────
# SKILL real do user (Context7 MCP Assistant)
# ────────────────────────────────────────────────────────────────

SKILL_CTX7_CUSTOM = """---
id: urn:skill:geral:subagent:context7-mcp
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
{"type":"object","required":["action","subject"],"properties":{"action":{"type":"string","enum":["get_documentation","update_prompt"]},"subject":{"type":"string","description":"Tema da consulta"},"content":{"type":"string","description":"Conteúdo extenso, multilinha. Aceita markdown e listas."}}}
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

CTX7_TOOL_REGISTRY = {
    "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
    "db_id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
    "name": "Context 7 MCP Server",
    "description": "Plataforma Context7",
    "operations": ["docs", "code", "prompt"],
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
}


class TestExtractInputsSchema:
    """Validação da cópia local de _extract_inputs_schema."""

    def test_extracts_schema_with_properties(self):
        from app.workspace.binding_schema import _extract_inputs_schema
        s = _extract_inputs_schema(SKILL_CTX7_CUSTOM)
        assert s is not None
        assert set(s["properties"].keys()) == {"action", "subject", "content"}
        assert s["required"] == ["action", "subject"]

    def test_returns_none_when_no_section(self):
        from app.workspace.binding_schema import _extract_inputs_schema
        assert _extract_inputs_schema("# X\n\n## Outra\n") is None

    def test_returns_none_when_empty(self):
        from app.workspace.binding_schema import _extract_inputs_schema
        assert _extract_inputs_schema("") is None


class TestFieldsFromJsonSchema:
    """Mapeamento JSON Schema → fields canônicos."""

    def test_preserves_properties_and_required(self):
        from app.workspace.binding_schema import _fields_from_json_schema
        schema = {
            "type": "object",
            "required": ["a"],
            "properties": {
                "a": {"type": "string", "description": "Field a"},
                "b": {"type": "integer"},
                "c": {"type": "boolean"},
            },
        }
        fields = _fields_from_json_schema(schema)
        names = {f["name"]: f for f in fields}
        assert names["a"]["required"] is True
        assert names["b"]["required"] is False
        assert names["b"]["type"] == "integer"
        assert names["c"]["type"] == "boolean"

    def test_enum_becomes_canonical_enum_type(self):
        from app.workspace.binding_schema import _fields_from_json_schema
        schema = {
            "properties": {"action": {"type": "string", "enum": ["x", "y"]}},
        }
        fields = _fields_from_json_schema(schema)
        assert fields[0]["type"] == "enum"
        assert fields[0]["enum"] == ["x", "y"]

    def test_infers_multiline_for_content_fields(self):
        """Heurística: nome contém content/body/text/payload → textarea."""
        from app.workspace.binding_schema import _fields_from_json_schema
        schema = {
            "properties": {
                "content": {"type": "string"},
                "subject": {"type": "string"},  # nome curto → não multiline
            },
        }
        fields = {f["name"]: f for f in _fields_from_json_schema(schema)}
        assert fields["content"]["multiline"] is True
        assert fields["subject"]["multiline"] is False


class TestFieldsFromOperations:
    """Fallback legacy {operation, query}."""

    def test_produces_operation_and_query_fields(self):
        from app.workspace.binding_schema import _fields_from_operations
        fields = _fields_from_operations(["docs", "code"])
        names = {f["name"] for f in fields}
        assert names == {"operation", "query"}
        op = next(f for f in fields if f["name"] == "operation")
        assert op["type"] == "enum"
        assert op["enum"] == ["docs", "code"]

    def test_handles_empty_operations(self):
        from app.workspace.binding_schema import _fields_from_operations
        fields = _fields_from_operations([])
        op = next(f for f in fields if f["name"] == "operation")
        assert op["enum"] is None  # sem enum, vira string livre


class TestNormalizeMcpBinding:
    """Orquestração: precedência SKILL > tool.inputSchema > legacy."""

    def test_prefers_skill_inputs_schema(self):
        """SKILL com ## Inputs custom dirige o schema, não tool.inputSchema
        nem operations."""
        from app.workspace.binding_schema import normalize_mcp_binding
        result = normalize_mcp_binding(CTX7_TOOL_REGISTRY, skill_md=SKILL_CTX7_CUSTOM)
        assert result["schema_source"] == "skill_inputs"
        field_names = {f["name"] for f in result["fields"]}
        # SKILL declarou action/subject/content — NÃO operation/query
        assert field_names == {"action", "subject", "content"}
        assert "operation" not in field_names
        assert "query" not in field_names

    def test_falls_back_to_tool_input_schema(self):
        """Sem SKILL ## Inputs útil, usa o que veio do MCP server discovery."""
        from app.workspace.binding_schema import normalize_mcp_binding
        result = normalize_mcp_binding(CTX7_TOOL_REGISTRY, skill_md="")
        assert result["schema_source"] == "mcp_input_schema"
        assert {f["name"] for f in result["fields"]} == {"query"}

    def test_legacy_fallback_when_no_schema_at_all(self):
        """Tool sem inputSchema E sem SKILL: fallback {operation, query}."""
        from app.workspace.binding_schema import normalize_mcp_binding
        tool = {"id": "x", "name": "Y", "operations": ["a", "b"], "inputSchema": None}
        result = normalize_mcp_binding(tool, skill_md=None)
        assert result["schema_source"] == "legacy_fallback"
        assert {f["name"] for f in result["fields"]} == {"operation", "query"}

    def test_preserves_binding_metadata(self):
        from app.workspace.binding_schema import normalize_mcp_binding
        result = normalize_mcp_binding(CTX7_TOOL_REGISTRY, skill_md=SKILL_CTX7_CUSTOM)
        assert result["binding_kind"] == "mcp"
        assert result["binding_id"] == "481c5fa3-36bc-4d05-97ff-d502d93521ff"
        assert result["binding_label"] == "Context 7 MCP Server"
        assert result["operations"] == ["docs", "code", "prompt"]

    def test_normalizes_operations_csv_string(self):
        """Operations às vezes vem como string CSV. Normaliza."""
        from app.workspace.binding_schema import normalize_mcp_binding
        tool = {"id": "x", "name": "T", "operations": ["docs,code,prompt"]}
        result = normalize_mcp_binding(tool, skill_md="")
        assert result["operations"] == ["docs", "code", "prompt"]


class TestValidateParams:
    """Validação payload do user vs CanonicalFormSchema."""

    def _schema(self):
        return {
            "fields": [
                {"name": "action", "type": "enum", "enum": ["x", "y"], "required": True},
                {"name": "subject", "type": "string", "required": True},
                {"name": "content", "type": "string", "required": False},
            ],
        }

    def test_ok_when_all_required_present(self):
        from app.workspace.binding_schema import validate_params_against_schema
        ok, errs = validate_params_against_schema(
            self._schema(),
            {"action": "x", "subject": "test"},
        )
        assert ok is True
        assert errs == []

    def test_fails_when_required_missing(self):
        from app.workspace.binding_schema import validate_params_against_schema
        ok, errs = validate_params_against_schema(
            self._schema(),
            {"action": "x"},  # falta subject
        )
        assert ok is False
        assert any("subject" in e for e in errs)

    def test_fails_when_required_empty_string(self):
        from app.workspace.binding_schema import validate_params_against_schema
        ok, errs = validate_params_against_schema(
            self._schema(),
            {"action": "x", "subject": "   "},
        )
        assert ok is False

    def test_fails_when_enum_value_invalid(self):
        from app.workspace.binding_schema import validate_params_against_schema
        ok, errs = validate_params_against_schema(
            self._schema(),
            {"action": "z", "subject": "test"},  # 'z' fora do enum
        )
        assert ok is False
        assert any("enum" in e.lower() for e in errs)


class TestRegressionContext7VPolicy:
    """Regressão end-to-end: SKILL Context7 v6 do user gera schema
    canônico com 3 fields reais — provando que slash command resolve a
    compressão {operation, query} do engine atual."""

    def test_skill_v6_produces_3_fields_not_2(self):
        from app.workspace.binding_schema import normalize_mcp_binding
        result = normalize_mcp_binding(CTX7_TOOL_REGISTRY, skill_md=SKILL_CTX7_CUSTOM)
        # 3 fields da SKILL: action, subject, content
        assert len(result["fields"]) == 3
        # Action é enum
        action = next(f for f in result["fields"] if f["name"] == "action")
        assert action["type"] == "enum"
        assert action["enum"] == ["get_documentation", "update_prompt"]
        # Content é multiline (description longa)
        content = next(f for f in result["fields"] if f["name"] == "content")
        assert content["multiline"] is True

    def test_engine_compression_now_avoided(self):
        """Antes (PR #195-#197): UI mostrava {operation, query} fixo.
        Agora: UI mostra o schema REAL da SKILL — user envia direto."""
        from app.workspace.binding_schema import normalize_mcp_binding
        result = normalize_mcp_binding(CTX7_TOOL_REGISTRY, skill_md=SKILL_CTX7_CUSTOM)
        # Causa raiz dos bugs Context7 #1-#5: schema custom comprimido em
        # {operation, query} pelo LLM. Aqui provamos que A.1 evita isso.
        field_names = {f["name"] for f in result["fields"]}
        assert field_names == {"action", "subject", "content"}
