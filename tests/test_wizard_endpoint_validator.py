"""Integração do validador no endpoint /api/v1/wizard/skill.

Testa o ciclo completo: LLM gera SKILL → parser → validador → retry se
crítico → response com validation dict.

Mocks: get_provider devolve fake LLM determinístico. _resolve_bindings é
mockado pra evitar lookup no DB. _resolve_wizard_llm mockado pra evitar
roteamento.

Cobertura:
- Geração inicial OK → 1 LLM call, retries_used=0, ok=True
- Geração inicial crítica → 2 LLM calls, retries_used=1
- Retry corrige → ok=True
- Retry ainda viola → ok=False, devolve retry + warnings
- Parser falha → segue sem validação (sem retry)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client():
    from app.main import app
    return TestClient(app)


# SKILL gerada com Workflow imperativo correto + operations válidas + Examples
# rastreáveis — passa em todas as regras do validador.
SKILL_OK = """---
id: urn:skill:geral:subagent:test
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Test Skill

## Purpose
Teste do validador integrado.

## Activation Criteria
Quando user pedir.

## Inputs
```json
{"type": "object"}
```

## Workflow
1. **Chame** a tool `Tool X` com `operation=docs` e `query=<entrada>` ANTES de gerar.
2. Use a resposta da tool para compor a saída.

## Tool Bindings
- `tool-1` (Tool X) — Tool de teste com operations docs/code.

## Output Contract
```json
{"type": "object"}
```

## Failure Modes
- ToolError: tentar novamente.

## Evidence Policy
A única fonte autorizada é o binding Tool X declarado em ## Tool Bindings.

## Guardrails
Sem PII.

## Examples
### Exemplo 1
**Entrada:** `{q: "test"}`
**Chamada à tool:** `Tool X` operation=`docs` query=`test`
**Resposta da tool:** retornou docs.
**Saída final:** `{result: ok}`
"""


# SKILL gerada com bug Context7 v2 — operation=search inventada
SKILL_V2_BUG = """---
id: urn:skill:geral:subagent:test
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Test Skill

## Purpose
Teste com bug v2.

## Activation Criteria
Quando user pedir.

## Inputs
```json
{"type": "object"}
```

## Workflow
1. **Chame** a tool `Tool X` com `operation=search` e `query=<entrada>` antes de gerar.

## Tool Bindings
- `tool-1` (Tool X) — Tool de teste.

## Output Contract
```json
{"type": "object"}
```

## Failure Modes
- Falha de teste.

## Evidence Policy
A única fonte autorizada é o binding Tool X.

## Guardrails
Sem PII.
"""


# Mesma estrutura mas com operation=docs (válida) — usado como retry output
SKILL_FIXED_FROM_V2 = SKILL_V2_BUG.replace("operation=search", "operation=docs")


def _patch_wizard_internals(mock_responses: list[str]):
    """Patches contextuais comuns aos testes do endpoint.

    Args:
        mock_responses: lista de strings que o fake LLM devolve em ordem.
            Cada chamada llm.generate() consome um item.

    Yields o stack de patches.
    """
    bindings_fixture = {
        "mcp_tools": [{
            "id": "tool-1",
            "name": "Tool X",
            "description": "Tool de teste",
            "operations": "docs,code",
        }],
        "rag_sources": [],
        "data_tables": [],
        "api_endpoints": [],
    }

    call_log = {"count": 0}

    class _FakeLLM:
        async def generate(self, messages, **kwargs):
            i = call_log["count"]
            call_log["count"] += 1
            if i >= len(mock_responses):
                # Reusa último — fallback defensivo se teste fez calls extras
                return {"content": mock_responses[-1]}
            return {"content": mock_responses[i]}

    return bindings_fixture, call_log, _FakeLLM()


class TestWizardEndpointWithValidator:

    @pytest.mark.asyncio
    async def test_clean_skill_no_retry_returns_ok_validation(self, app_client):
        bindings, call_log, fake_llm = _patch_wizard_internals([SKILL_OK])
        with patch("app.routes.wizard._resolve_bindings_for_prompt",
                   AsyncMock(return_value=bindings)), \
             patch("app.routes.wizard._resolve_wizard_llm",
                   AsyncMock(return_value=("openai", "gpt-4o", "reasoning"))), \
             patch("app.routes.wizard.get_provider", return_value=fake_llm):
            r = app_client.post("/api/v1/wizard/skill", json={
                "description": "skill teste",
                "mcp_tool_ids": ["tool-1"],
            })
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # SKILL gerada OK → 1 LLM call, sem retry
        assert call_log["count"] == 1
        # Response inclui validation dict
        assert "validation" in data
        assert data["validation"]["ok"] is True
        assert data["validation"]["retries_used"] == 0
        assert data["validation"]["critical_count"] == 0

    @pytest.mark.asyncio
    async def test_critical_violation_triggers_retry(self, app_client):
        # Primeira call: bug v2. Segunda: corrigida.
        bindings, call_log, fake_llm = _patch_wizard_internals([
            SKILL_V2_BUG, SKILL_FIXED_FROM_V2,
        ])
        with patch("app.routes.wizard._resolve_bindings_for_prompt",
                   AsyncMock(return_value=bindings)), \
             patch("app.routes.wizard._resolve_wizard_llm",
                   AsyncMock(return_value=("openai", "gpt-4o", "reasoning"))), \
             patch("app.routes.wizard.get_provider", return_value=fake_llm):
            r = app_client.post("/api/v1/wizard/skill", json={
                "description": "skill teste",
                "mcp_tool_ids": ["tool-1"],
            })
        assert r.status_code == 200
        data = r.json()
        # 2 LLM calls (geração + retry)
        assert call_log["count"] == 2
        # SKILL final é a corrigida (retry com operation=docs)
        assert "operation=docs" in data["skill_md"]
        assert "operation=search" not in data["skill_md"]
        # Validação final ok
        assert data["validation"]["ok"] is True
        assert data["validation"]["retries_used"] == 1

    @pytest.mark.asyncio
    async def test_retry_still_violates_returns_warnings(self, app_client):
        """LLM gerador é incapaz — viola na geração inicial E no retry.
        Endpoint não chama 3x (retries_used=1), retorna a SKILL do retry
        com violations no payload pro frontend mostrar."""
        bindings, call_log, fake_llm = _patch_wizard_internals([
            SKILL_V2_BUG, SKILL_V2_BUG,  # retry também viola
        ])
        with patch("app.routes.wizard._resolve_bindings_for_prompt",
                   AsyncMock(return_value=bindings)), \
             patch("app.routes.wizard._resolve_wizard_llm",
                   AsyncMock(return_value=("openai", "gpt-4o", "reasoning"))), \
             patch("app.routes.wizard.get_provider", return_value=fake_llm):
            r = app_client.post("/api/v1/wizard/skill", json={
                "description": "skill teste",
                "mcp_tool_ids": ["tool-1"],
            })
        assert r.status_code == 200
        data = r.json()
        assert call_log["count"] == 2  # retry cap = 1
        assert data["validation"]["ok"] is False
        assert data["validation"]["retries_used"] == 1
        # Pelo menos a violação operation.invented está no payload
        rules = {v["rule"] for v in data["validation"]["violations"]}
        assert "operation.invented" in rules

    @pytest.mark.asyncio
    async def test_no_bindings_no_validation_blocks(self, app_client):
        """Skill puramente de raciocínio (sem bindings) — validador roda
        mas não vai disparar críticos."""
        bindings = {
            "mcp_tools": [], "rag_sources": [],
            "data_tables": [], "api_endpoints": [],
        }
        skill_md = """---
id: urn:skill:geral:subagent:reasoning
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Reasoning Skill

## Purpose
Pura razão.

## Activation Criteria
Sempre.

## Inputs
```json
{"type": "object"}
```

## Workflow
Geração de resposta com base em raciocínio próprio.

## Tool Bindings
_Esta skill não usa ferramentas MCP._

## Output Contract
```json
{"type": "object"}
```

## Failure Modes
Erro de raciocínio.
"""
        _, call_log, fake_llm = _patch_wizard_internals([skill_md])
        with patch("app.routes.wizard._resolve_bindings_for_prompt",
                   AsyncMock(return_value=bindings)), \
             patch("app.routes.wizard._resolve_wizard_llm",
                   AsyncMock(return_value=("openai", "gpt-4o", "reasoning"))), \
             patch("app.routes.wizard.get_provider", return_value=fake_llm):
            r = app_client.post("/api/v1/wizard/skill", json={"description": "x"})
        assert r.status_code == 200
        data = r.json()
        assert call_log["count"] == 1
        assert data["validation"]["ok"] is True
        # Não dispara críticos quando não há binding
        assert data["validation"]["critical_count"] == 0

    @pytest.mark.asyncio
    async def test_parser_failure_does_not_block_response(self, app_client):
        """SKILL com YAML malformado — parser falha. Endpoint segue sem
        validação (não dá pra retry sem saber o que tá errado), retorna a
        SKILL como veio do LLM com warning logado."""
        bad_skill = "isto não é uma SKILL válida\nnem frontmatter nem sections"
        bindings, call_log, fake_llm = _patch_wizard_internals([bad_skill])
        with patch("app.routes.wizard._resolve_bindings_for_prompt",
                   AsyncMock(return_value=bindings)), \
             patch("app.routes.wizard._resolve_wizard_llm",
                   AsyncMock(return_value=("openai", "gpt-4o", "reasoning"))), \
             patch("app.routes.wizard.get_provider", return_value=fake_llm):
            r = app_client.post("/api/v1/wizard/skill", json={
                "description": "x",
                "mcp_tool_ids": ["tool-1"],
            })
        # Não bloqueia — operador recebe a SKILL pra editar
        assert r.status_code == 200
        data = r.json()
        assert data["skill_md"] == bad_skill
        # Sem retry quando parser falhou (retries_used 0 ou ausência de validation)
        assert call_log["count"] == 1
        # Não obrigatório ter 'validation' no payload — pode ter sido None
        if "validation" in data:
            assert data["validation"].get("retries_used", 0) == 0
