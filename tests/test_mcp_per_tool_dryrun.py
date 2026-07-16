"""Dry-run PER-TOOL completo (39.x — item 3 PR5).

O PR4 deixou só um AVISO ("a simulação per-tool vem no PR5"). Agora o dry-run
SIMULA o caminho real: função descoberta + args crus. Simular o legado num
conector per-tool mostrava ao operador um contrato que o runtime não pratica.
"""
from __future__ import annotations

import json

import pytest

import app.routes.skill_dryrun as dr


_DISC = json.dumps([
    {"name": "web_search", "description": "busca",
     "inputSchema": {"type": "object", "required": ["q"],
                     "properties": {"q": {"type": "string"},
                                    "max_results": {"type": "integer"}}}},
    {"name": "extract_page", "description": "extrai",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
])

_TID = "11111111-1111-1111-1111-111111111111"

_SKILL_COM_INPUTS = (
    "---\nid: urn:skill:x\nversion: 0.1.0\nkind: subagent\n---\n# S\n"
    "## Purpose\nBuscar.\n"
    "## Inputs\n```json\n{\"type\":\"object\",\"properties\":{\"assunto\":{\"type\":\"string\"}}}\n```\n"
    "## Workflow\n1. **Chame** a tool com operation=search e query=<x>.\n"
    f"## Tool Bindings\n- `{_TID}` (Tavily)\n"
)


def _tool(**over):
    base = {"id": _TID, "name": "Tavily", "operations": "search",
            "discovered_tools": _DISC, "per_tool_mode": "on",
            "mcp_server": "http://mcp:3001", "description": "busca"}
    base.update(over)
    return base


@pytest.fixture
def registry(monkeypatch):
    """Resolve o conector sem tocar o DB; `per_tool_mode='on'` dispensa o global."""
    def _install(tool):
        async def _resolve(tool_id):
            return dict(tool, id=tool_id)
        monkeypatch.setattr(dr, "_resolve_tool_from_registry", _resolve)
    return _install


class TestSimulacaoPerTool:
    @pytest.mark.asyncio
    async def test_devolve_funcao_real_e_payload_cru(self, registry):
        registry(_tool())
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID,
            extra_params={"q": "maestro", "max_results": 3},
        ))
        assert res.per_tool_active is True
        assert res.tool_name_resolved == "web_search"
        assert res.function_spec["_mcp_real_name"] == "web_search"
        # Payload CRU: nada de operation injetada, nada de {operation, query}
        assert res.payload_that_would_be_sent == {"q": "maestro", "max_results": 3}
        assert "operation" not in res.payload_that_would_be_sent
        assert res.operation_resolved == ""
        assert res.issues[0].rule == "per_tool.simulated"
        assert res.per_tool_available == ["web_search", "extract_page"]

    @pytest.mark.asyncio
    async def test_omite_skill_declared_spec_mesmo_com_inputs(self, registry):
        """O JS usa `skillSpec || engineSpec`. Devolver o spec da SKILL faria o
        form ser dirigido pelo ## Inputs e IGNORAR o schema real descoberto —
        o contrário do que este modo prova."""
        registry(_tool())
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, extra_params={"q": "x"},
        ))
        assert res.function_spec_skill_declared is None

    @pytest.mark.asyncio
    async def test_sem_checagens_do_legado(self, registry):
        """Conector per-tool não usa `operations` do Registry e seu schema vem
        do inputSchema — as checagens legadas viram ruído contra contrato correto."""
        registry(_tool(operations=""))
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, extra_params={"q": "x"},
        ))
        rules = [i.rule for i in res.issues]
        assert "dryrun.no_operations_in_registry" not in rules
        assert "schema.mismatch" not in rules
        assert "dryrun.operation_not_in_enum" not in rules
        assert res.ok is True

    @pytest.mark.asyncio
    async def test_validador_estatico_continua_rodando(self, registry):
        """Qualidade da SKILL é ORTOGONAL ao transporte: gatear o validador
        inteiro no modo per-tool cegaria G1-G4 e os guardrails. Aqui o Workflow
        é PASSIVO (sem verbo imperativo) — o validador tem que continuar
        acusando isso mesmo com o conector em per-tool."""
        registry(_tool())
        skill_passiva = _SKILL_COM_INPUTS.replace(
            "1. **Chame** a tool com operation=search e query=<x>.",
            "1. A busca deve ser realizada pelo sistema conforme necessário.",
        )
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=skill_passiva, tool_id=_TID, extra_params={"q": "x"},
        ))
        estaticas = [i.rule for i in res.issues
                     if not i.rule.startswith(("dryrun.", "schema.", "per_tool."))]
        assert estaticas, (
            "validador estático foi silenciado junto com as checagens do legado"
        )

    @pytest.mark.asyncio
    async def test_tool_name_vazio_usa_a_primeira(self, registry):
        registry(_tool())
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, tool_name="",
        ))
        assert res.tool_name_resolved == "web_search"

    @pytest.mark.asyncio
    async def test_seleciona_a_tool_pedida(self, registry):
        registry(_tool())
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, tool_name="extract_page",
            extra_params={"url": "http://x"},
        ))
        assert res.tool_name_resolved == "extract_page"
        assert res.function_spec["function"]["name"].endswith("extract_page")

    @pytest.mark.asyncio
    async def test_casa_pelo_nome_sanitizado(self, registry):
        """O front pode ter só o `function.name` (sanitizado)."""
        registry(_tool())
        from app.mcp.runtime import build_per_tool_openai_functions, _parse_discovered_tools
        sanitized = build_per_tool_openai_functions(
            _tool(), _parse_discovered_tools(_DISC))[1]["function"]["name"]
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, tool_name=sanitized,
        ))
        assert res.tool_name_resolved == "extract_page"


class TestToolInexistente:
    @pytest.mark.asyncio
    async def test_e_critical_e_derruba_o_ok(self, registry):
        """O `ok` era calculado ANTES do concat das issues per-tool — um dry-run
        reprovado se anunciaria aprovado."""
        registry(_tool())
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID, tool_name="fantasma",
        ))
        assert res.issues[0].rule == "per_tool.tool_not_found"
        assert res.issues[0].severity == "critical"
        assert res.ok is False
        assert "web_search" in res.issues[0].suggestion   # acionável
        # Não anuncia ter simulado per-tool quando não simulou.
        assert res.per_tool_active is False


class TestLegadoIntacto:
    @pytest.mark.asyncio
    async def test_modo_off_segue_no_operation_query(self, registry, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "0")
        registry(_tool(per_tool_mode="off"))
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID,
        ))
        assert res.per_tool_active is False
        assert res.tool_name_resolved == ""
        assert res.operation_resolved == "search"
        assert res.payload_that_would_be_sent["operation"] == "search"
        assert "query" in res.payload_that_would_be_sent
        assert all(i.rule != "per_tool.simulated" for i in res.issues)

    @pytest.mark.asyncio
    async def test_sem_descoberta_cai_no_legado(self, registry, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        registry(_tool(discovered_tools=None))
        res = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=_SKILL_COM_INPUTS, tool_id=_TID,
        ))
        assert res.per_tool_active is False
        assert res.payload_that_would_be_sent.get("operation") == "search"


class TestBackCompat:
    def test_campos_novos_tem_default(self):
        r = dr.DryRunResult(ok=True, payload_that_would_be_sent={},
                            function_spec={}, issues=[], operation_resolved="x")
        assert r.per_tool_active is False
        assert r.tool_name_resolved == ""
        assert r.per_tool_available == []

    def test_request_sem_tool_name_e_valida(self):
        assert dr.DryRunRequest(skill_md="x", tool_id="y").tool_name is None
