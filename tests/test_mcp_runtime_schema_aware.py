"""Onda B — build_openai_tools schema-aware no caminho NL (LLM-driven).

Antes: build_openai_tools sempre produzia `{operation, query}` fixo.
Mesmo com SKILL declarando `## Inputs: {action, subject, content}`, o LLM
via apenas `{operation, query}` e tentava comprimir os 3 campos em 2 —
causa raiz dos bugs Context7 #1-#5 no caminho NL.

Onda B: build_openai_tools(mcp_tools, skill_md=None) lê SKILL `## Inputs`
quando presente e usa ESSE schema pro LLM. Fallback legacy quando ausente.

Cobertura:
- Legacy path (sem skill_md OU sem ## Inputs parseável)
- Schema-aware path (com ## Inputs válido)
- Operations enum injection em schemas custom que declaram `operation`
- Precedência: SKILL ## Inputs > tool.inputSchema > legacy
- _schema_origin metadata pra observability
- Regressão: Context7 v6 do user agora produz spec com {action, subject,
  content} no caminho NL — não só no slash (A.1)
"""
from __future__ import annotations


# ─────────────────────────────────────────────────────────────────
# Helper canônico — uma fonte de verdade pra _extract_inputs_schema
# ─────────────────────────────────────────────────────────────────


class TestCanonicalExtractor:
    def test_extracts_schema_from_inputs_section(self):
        from app.skill_parser.inputs_schema import extract_inputs_schema
        md = """# X

## Inputs
```json
{"type":"object","required":["a"],"properties":{"a":{"type":"string"}}}
```

## Next
"""
        s = extract_inputs_schema(md)
        assert s is not None
        assert s["required"] == ["a"]
        assert s["properties"]["a"]["type"] == "string"

    def test_returns_none_for_empty_input(self):
        from app.skill_parser.inputs_schema import extract_inputs_schema
        assert extract_inputs_schema("") is None
        assert extract_inputs_schema(None) is None  # type: ignore[arg-type]

    def test_returns_none_for_schema_without_properties(self):
        """Schema sem properties não vira function spec útil — engine cai
        no fallback legacy."""
        from app.skill_parser.inputs_schema import extract_inputs_schema
        md = """# X
## Inputs
```json
{"type":"object"}
```
## Next
"""
        assert extract_inputs_schema(md) is None


# ─────────────────────────────────────────────────────────────────
# Onda B: build_openai_tools com skill_md
# ─────────────────────────────────────────────────────────────────


CTX7_TOOL = {
    "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
    "name": "Context 7 MCP Server",
    "operations": ["docs", "code", "prompt"],
    "description": "Documentação Context7",
}


SKILL_CTX7_WITH_INPUTS = """---
id: urn:skill:geral:subagent:ctx7
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Context7 Assistant

## Inputs
```json
{"type":"object","required":["action","subject"],"properties":{"action":{"type":"string","enum":["get_documentation","update_prompt"]},"subject":{"type":"string"},"content":{"type":"string"}}}
```

## Workflow
1. Chame a tool.

## Tool Bindings
- `481c5fa3-36bc-4d05-97ff-d502d93521ff` (Context 7 MCP Server) — desc.
"""


SKILL_NO_INPUTS = """---
id: urn:skill:geral:subagent:no-inputs
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---

# Sem Inputs

## Inputs
texto livre sem schema parseável

## Workflow
1. X.

## Tool Bindings
- `aaa` (Tool) — desc.
"""


class TestBuildOpenaiToolsLegacyPath:
    """Sem skill_md OU com ## Inputs vazio — comportamento pré-Onda B preservado."""

    def test_no_skill_md_yields_operation_query_schema(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL])  # sem skill_md
        spec = tools[0]
        props = spec["function"]["parameters"]["properties"]
        assert set(props.keys()) == {"operation", "query"}
        # Enum vem das operations do Registry
        assert props["operation"]["enum"] == ["docs", "code", "prompt"]
        # Origem rastreada pra observability
        assert spec["_schema_origin"] == "legacy_operation_query"

    def test_skill_md_without_inputs_falls_back_to_legacy(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL], skill_md=SKILL_NO_INPUTS)
        spec = tools[0]
        # SKILL ## Inputs sem schema parseável → fallback legacy
        props = spec["function"]["parameters"]["properties"]
        assert set(props.keys()) == {"operation", "query"}
        assert spec["_schema_origin"] == "legacy_operation_query"

    def test_required_remains_operation_query_in_legacy(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL])
        assert tools[0]["function"]["parameters"]["required"] == ["operation", "query"]


class TestBuildOpenaiToolsSchemaAware:
    """Com skill_md trazendo ## Inputs parseável — Onda B."""

    def test_uses_skill_inputs_schema_when_present(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS)
        spec = tools[0]
        props = spec["function"]["parameters"]["properties"]
        # LLM agora vê os fields REAIS da SKILL — não compressed em {operation, query}
        assert set(props.keys()) == {"action", "subject", "content"}
        assert spec["_schema_origin"] == "skill_inputs"

    def test_required_propagates_from_skill_inputs(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS)
        req = tools[0]["function"]["parameters"]["required"]
        assert req == ["action", "subject"]

    def test_enum_preserved_from_skill_inputs(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS)
        action = tools[0]["function"]["parameters"]["properties"]["action"]
        assert action["enum"] == ["get_documentation", "update_prompt"]

    def test_description_unchanged_by_schema_choice(self):
        """A description rica é responsável pela DECISÃO do LLM de invocar
        a tool — Onda B mexe só no schema, não na decisão."""
        from app.mcp.runtime import build_openai_tools
        legacy = build_openai_tools([CTX7_TOOL])
        aware = build_openai_tools([CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS)
        assert legacy[0]["function"]["description"] == aware[0]["function"]["description"]


class TestOperationsEnumInjection:
    """Quando SKILL declara `operation` como string sem enum, mas Registry
    tem operations → injetamos enum (Registry sabe mais que SKILL).
    Quando SKILL declara enum próprio, preservamos."""

    def test_injects_enum_when_skill_has_operation_string_no_enum(self):
        from app.mcp.runtime import build_openai_tools
        skill_with_op_no_enum = """---
id: x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---
# X

## Inputs
```json
{"type":"object","required":["operation"],"properties":{"operation":{"type":"string","description":"qual"},"q":{"type":"string"}}}
```
"""
        tools = build_openai_tools([CTX7_TOOL], skill_md=skill_with_op_no_enum)
        op = tools[0]["function"]["parameters"]["properties"]["operation"]
        # Injetamos enum do Registry
        assert op["enum"] == ["docs", "code", "prompt"]

    def test_preserves_skill_declared_enum(self):
        """Quando SKILL declara enum próprio, NÃO sobrescreve com Registry."""
        from app.mcp.runtime import build_openai_tools
        skill_with_own_enum = """---
id: x
version: 0.1.0
kind: subagent
owner: e
stability: alpha
---
# X

## Inputs
```json
{"type":"object","properties":{"operation":{"type":"string","enum":["custom"]}}}
```
"""
        tools = build_openai_tools([CTX7_TOOL], skill_md=skill_with_own_enum)
        op = tools[0]["function"]["parameters"]["properties"]["operation"]
        # Mantém enum da SKILL (autor sabe o contexto melhor que Registry agora)
        assert op["enum"] == ["custom"]


class TestSchemaOriginPrecedence:
    """Precedência: SKILL ## Inputs > tool.inputSchema > legacy."""

    def test_tool_input_schema_used_when_skill_md_absent(self):
        """tool.inputSchema vem de MCP discovery (Onda B.2 vai popular).
        Quando manualmente populado E sem skill_md → vira a fonte."""
        from app.mcp.runtime import build_openai_tools
        tool_with_input_schema = {
            **CTX7_TOOL,
            "inputSchema": {
                "type": "object",
                "required": ["libraryName"],
                "properties": {
                    "libraryName": {"type": "string"},
                    "topic": {"type": "string"},
                },
            },
        }
        tools = build_openai_tools([tool_with_input_schema])
        spec = tools[0]
        props = spec["function"]["parameters"]["properties"]
        assert set(props.keys()) == {"libraryName", "topic"}
        assert spec["_schema_origin"] == "tool_input_schema"

    def test_skill_inputs_wins_over_tool_input_schema(self):
        """SKILL ## Inputs é o contrato do autor — prevalece sobre o
        que o servidor MCP declara (pode estar desatualizado, ou autor
        quer expor subset)."""
        from app.mcp.runtime import build_openai_tools
        tool_with_input_schema = {
            **CTX7_TOOL,
            "inputSchema": {
                "type": "object",
                "properties": {"libraryName": {"type": "string"}},
            },
        }
        tools = build_openai_tools(
            [tool_with_input_schema], skill_md=SKILL_CTX7_WITH_INPUTS,
        )
        spec = tools[0]
        # SKILL ## Inputs prevalece
        assert set(spec["function"]["parameters"]["properties"].keys()) == {
            "action", "subject", "content",
        }
        assert spec["_schema_origin"] == "skill_inputs"


# ─────────────────────────────────────────────────────────────────
# Regressão arquitetural: Context7 NL path agora funciona
# ─────────────────────────────────────────────────────────────────


class TestRegressionContext7NLPath:
    """Bugs Context7 #1-#5 NO CAMINHO NL: SKILL declarava {action, subject,
    content} mas LLM via {operation, query} e errava. Onda A.1 resolveu pra
    slash; Onda B resolve pra NL também."""

    def test_ctx7_nl_path_now_exposes_real_schema(self):
        from app.mcp.runtime import build_openai_tools
        tools = build_openai_tools([CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS)
        params = tools[0]["function"]["parameters"]
        # Schema que o LLM REALMENTE vê — sem compressão
        assert set(params["properties"].keys()) == {"action", "subject", "content"}
        assert params["required"] == ["action", "subject"]

    def test_engine_path_aligned_with_slash_path(self):
        """Onda A.1 slash uses canonical schema from binding_schema.py.
        Onda B engine uses canonical schema from inputs_schema.py.
        Both should produce the same set of fields pro mesmo SKILL.md."""
        from app.mcp.runtime import build_openai_tools
        from app.workspace.binding_schema import normalize_mcp_binding

        engine_spec = build_openai_tools(
            [CTX7_TOOL], skill_md=SKILL_CTX7_WITH_INPUTS,
        )[0]
        slash_spec = normalize_mcp_binding(CTX7_TOOL, skill_md=SKILL_CTX7_WITH_INPUTS)

        engine_fields = set(engine_spec["function"]["parameters"]["properties"].keys())
        slash_fields = {f["name"] for f in slash_spec["fields"]}
        assert engine_fields == slash_fields == {"action", "subject", "content"}


# ─────────────────────────────────────────────────────────────────
# Helper engine.py — DeepAgentHarness aceita skill_md
# ─────────────────────────────────────────────────────────────────


class TestDeepAgentHarnessAcceptsSkillMd:
    """Smoke do contrato: DeepAgentHarness aceita skill_md, propaga pra
    build_openai_tools. Não testa execução end-to-end (requer LLM mock)."""

    def test_harness_init_with_skill_md_propagates_to_openai_tools(self):
        from app.agents.engine import DeepAgentHarness
        agent = {"id": "a1", "name": "T", "model": "gpt-4o-mini", "llm_provider": "openai"}
        harness = DeepAgentHarness(
            agent_config=agent,
            max_iterations=1,
            mcp_tools=[CTX7_TOOL],
            skill_md=SKILL_CTX7_WITH_INPUTS,
        )
        # openai_tools deve ter usado SKILL ## Inputs
        assert len(harness.openai_tools) == 1
        params = harness.openai_tools[0]["function"]["parameters"]
        assert set(params["properties"].keys()) == {"action", "subject", "content"}

    def test_harness_init_without_skill_md_preserves_legacy(self):
        from app.agents.engine import DeepAgentHarness
        agent = {"id": "a1", "name": "T", "model": "gpt-4o-mini", "llm_provider": "openai"}
        harness = DeepAgentHarness(
            agent_config=agent,
            max_iterations=1,
            mcp_tools=[CTX7_TOOL],
            # sem skill_md
        )
        params = harness.openai_tools[0]["function"]["parameters"]
        assert set(params["properties"].keys()) == {"operation", "query"}
