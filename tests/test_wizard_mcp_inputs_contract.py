"""Wizard: ## Inputs de skill MCP é forçado ao contrato {operation, query}.

CONTEXTO (bug "tavily a", 2026-06-08): o wizard manda o LLM gerar o SKILL.md, mas
só amarra `operation`/`query` no Workflow/Examples — o `## Inputs` (JSON Schema)
fica livre. O LLM modela inputs de DOMÍNIO pela finalidade da skill (ex.: 'Pontos
Turísticos' → `address`/`radius_meters`), e é esse schema que vira o contrato da
tool em runtime. Sem `operation`, o runtime usa o NOME DO SERVIDOR como tool →
o servidor MCP responde "Unknown tool" → bolha vazia.

Fix (determinístico, correto por construção): se a skill vincula tool MCP e o
`## Inputs` NÃO declara `operation`, o wizard substitui o `## Inputs` pelo
contrato canônico `{operation, query}` que o runtime entende (o enum das ops é
injetado em runtime a partir do Registry). Cobre o caso sem depender do LLM.
"""
from __future__ import annotations

from app.routes.wizard import _ensure_mcp_inputs_contract, _inputs_has_operation

MCP = [{"id": "t1", "name": "Tavily MCP Server", "description": "web search"}]

_BAD = '''---
name: tavily-a
---
## Purpose
Busca pontos turísticos.

## Inputs
```json
{
  "type": "object",
  "properties": {
    "address": {"type": "string"},
    "radius_meters": {"type": "integer", "default": 2000}
  },
  "required": ["address"]
}
```

## Workflow
Chame a tool.
'''

_GOOD = '''---
name: tavily-a
---
## Purpose
Busca web.

## Inputs
```json
{
  "type": "object",
  "properties": {
    "operation": {"type": "string"},
    "query": {"type": "string"}
  },
  "required": ["operation", "query"]
}
```

## Workflow
Chame a tool.
'''


def test_injects_operation_when_missing_and_mcp_bound():
    out = _ensure_mcp_inputs_contract(_BAD, MCP)
    assert _inputs_has_operation(out)
    assert '"operation"' in out and '"query"' in out
    assert '"radius_meters"' not in out and '"address"' not in out  # domínio substituído
    # demais seções preservadas
    assert "## Purpose" in out and "## Workflow" in out


def test_leaves_skill_unchanged_when_operation_present():
    assert _inputs_has_operation(_GOOD)            # sanity
    assert _ensure_mcp_inputs_contract(_GOOD, MCP) == _GOOD  # já certo → não toca


def test_no_change_without_mcp_tools():
    assert _ensure_mcp_inputs_contract(_BAD, []) == _BAD  # sem MCP, não mexe


def test_no_inputs_section_left_unchanged():
    """Replace-only: conteúdo sem ## Inputs (lixo / parse error) NÃO é tocado —
    o parser/validador lida. Evita anexar schema em não-skill."""
    garbage = "isto não é uma skill válida, sem frontmatter nem sections"
    assert _ensure_mcp_inputs_contract(garbage, MCP) == garbage


def test_inputs_has_operation_false_for_domain_schema():
    assert _inputs_has_operation(_BAD) is False
