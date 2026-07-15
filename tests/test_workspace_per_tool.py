"""Workspace + dry-run PER-TOOL (39.3.0 — item 3 PR4 do plano).

Os "gêmeos declarativos" fora do engine só conheciam {operation, query}.
Agora: conector em modo per-tool EFETIVO lista no Workspace 1 form POR TOOL
DESCOBERTA (campos do inputSchema real; binding_id composto db_id::real_name)
e o invoke direto encaminha pelo nome real via F3 (args crus, sem
operation/query e sem round-trip de re-descoberta). O dry-run legado avisa
quando o conector está em modo per-tool (a simulação completa vem no PR5).
"""
from __future__ import annotations

import json

import pytest

from app.workspace.binding_schema import (
    normalize_mcp_per_tool_bindings, validate_params_against_schema,
)


_DISCOVERED = json.dumps([
    {"name": "web_search", "description": "busca na web",
     "inputSchema": {"type": "object", "required": ["q"],
                     "properties": {"q": {"type": "string", "description": "consulta"},
                                    "max_results": {"type": "integer"}}}},
    {"name": "extract_page", "description": "extrai página",
     "inputSchema": {"type": "object",
                     "properties": {"url": {"type": "string"}}}},
])


def _tool(**over):
    # operations em CSV string: shape do Registry (dry-run faz .strip());
    # o normalizador do workspace aceita os dois.
    base = {"db_id": "t1", "id": "t1", "name": "Tavily", "operations": "search",
            "discovered_tools": _DISCOVERED, "per_tool_mode": "on",
            "mcp_server": "http://mcp:3001", "description": "busca"}
    base.update(over)
    return base


class TestFormsPorToolDescoberta:
    def test_um_form_por_tool_com_campos_reais(self):
        forms = normalize_mcp_per_tool_bindings(_tool())
        assert [f["binding_id"] for f in forms] == ["t1::web_search", "t1::extract_page"]
        f0 = forms[0]
        assert f0["schema_source"] == "discovered_per_tool"
        assert f0["per_tool"]["real_name"] == "web_search"
        names = {fld["name"]: fld for fld in f0["fields"]}
        assert names["q"]["required"] is True          # required do servidor
        assert names["max_results"]["type"] == "integer"
        assert "operation" not in names                # o par legado não existe

    def test_validacao_usa_o_required_do_servidor(self):
        forms = normalize_mcp_per_tool_bindings(_tool())
        ok, errors = validate_params_against_schema(forms[0], {})
        assert ok is False and any("'q'" in e for e in errors)
        ok, _ = validate_params_against_schema(forms[0], {"q": "maestro"})
        assert ok is True


class TestListaEInvokeDoWorkspace:
    """Fluxo real via rotas, com repos/execução mockados."""

    def _client(self, monkeypatch, capture):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app.routes.workspace as ws
        from app.core.auth import require_user

        skill_md = (
            "---\nid: urn:skill:x\nversion: 0.1.0\nkind: subagent\n---\n"
            "# S\n## Purpose\nBuscar.\n## Workflow\n1. **Chame** a tool.\n"
            "## Tool Bindings\n- `Tavily`\n"
        )

        async def _async(v):
            return v

        # repos são lazy-imports de app.core.database dentro dos handlers
        import app.core.database as db
        monkeypatch.setattr(db.agents_repo, "find_by_id",
                            lambda aid: _async({"id": aid, "name": "A", "skill_id": "s1"}))
        monkeypatch.setattr(db.skills_repo, "find_by_id",
                            lambda sid: _async({"id": sid, "name": "S", "raw_content": skill_md}))

        async def _match(parsed_tools, repo):
            # shape ENRIQUECIDO (match_with_registry): operations como LISTA
            return [_tool(operations=["search"])]

        monkeypatch.setattr("app.mcp.runtime.match_with_registry", _match)

        async def _exec(tool_name, arguments, mcp_tools, timeout=60, openai_tools=None):
            capture.update(tool_name=tool_name, arguments=arguments,
                           openai_tools=openai_tools)
            return json.dumps({"results": ["ok"]})

        monkeypatch.setattr("app.mcp.runtime.execute_tool_call", _exec)

        app = FastAPI()
        app.include_router(ws.router)
        app.dependency_overrides[require_user] = lambda: {"id": "u1"}
        return TestClient(app, raise_server_exceptions=False)

    def test_invoke_composto_encaminha_pelo_nome_real(self, monkeypatch):
        cap = {}
        client = self._client(monkeypatch, cap)
        r = client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": "a1", "skill_id": "s1", "binding_kind": "mcp",
            "binding_id": "t1::web_search", "params": {"q": "maestro"},
        })
        assert r.status_code == 200, r.text
        # F3: nome da FUNÇÃO per-tool + args CRUS (sem operation/query)
        assert cap["arguments"] == {"q": "maestro"}
        assert "operation" not in cap["arguments"]
        assert cap["openai_tools"] and \
            cap["openai_tools"][0]["_mcp_real_name"] == "web_search"
        assert cap["tool_name"] == cap["openai_tools"][0]["function"]["name"]

    def test_invoke_composto_valida_required_do_servidor(self, monkeypatch):
        cap = {}
        client = self._client(monkeypatch, cap)
        r = client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": "a1", "skill_id": "s1", "binding_kind": "mcp",
            "binding_id": "t1::web_search", "params": {},
        })
        assert r.status_code == 422, r.text
        assert "tool_name" not in cap  # nada executou

    def test_tool_nao_descoberta_404_acionavel(self, monkeypatch):
        cap = {}
        client = self._client(monkeypatch, cap)
        r = client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": "a1", "skill_id": "s1", "binding_kind": "mcp",
            "binding_id": "t1::fantasma", "params": {"q": "x"},
        })
        assert r.status_code == 404, r.text
        assert "re-teste a conexão" in r.json()["detail"]

    def test_binding_simples_continua_no_caminho_legado(self, monkeypatch):
        cap = {}
        client = self._client(monkeypatch, cap)
        r = client.post("/api/v1/workspace/invoke-binding-direct", json={
            "agent_id": "a1", "skill_id": "s1", "binding_kind": "mcp",
            "binding_id": "t1", "operation": "search",
            "params": {"operation": "search", "query": "maestro"},
        })
        assert r.status_code == 200, r.text
        assert cap["arguments"].get("operation") == "search"
        assert cap["openai_tools"] is None
        assert cap["tool_name"] == "Tavily"


class TestDryRunAvisaModoPerTool:
    @pytest.mark.asyncio
    async def test_info_quando_per_tool_efetivo(self, monkeypatch):
        import app.routes.skill_dryrun as dr

        async def _resolve(tool_id):
            return _tool(id=tool_id)

        monkeypatch.setattr(dr, "_resolve_tool_from_registry", _resolve)
        skill_md = (
            "---\nid: urn:skill:x\nversion: 0.1.0\nkind: subagent\n---\n# S\n"
            "## Purpose\nBuscar.\n## Workflow\n1. **Chame** a tool com "
            "operation=search e query=<x>.\n## Tool Bindings\n"
            "- `11111111-1111-1111-1111-111111111111` (Tavily)\n"
        )
        result = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=skill_md, tool_id="11111111-1111-1111-1111-111111111111",
        ))
        rules = [i.rule for i in result.issues]
        assert rules[0] == "per_tool.mode_active"  # PRIMEIRO — muda a leitura
        info = result.issues[0]
        assert "`web_search`" in info.message and info.severity == "info"

    @pytest.mark.asyncio
    async def test_sem_per_tool_sem_aviso(self, monkeypatch):
        import app.routes.skill_dryrun as dr

        async def _resolve(tool_id):
            return _tool(id=tool_id, per_tool_mode="off")

        monkeypatch.setattr(dr, "_resolve_tool_from_registry", _resolve)
        skill_md = (
            "---\nid: urn:skill:x\nversion: 0.1.0\nkind: subagent\n---\n# S\n"
            "## Purpose\nBuscar.\n## Workflow\n1. **Chame** a tool com "
            "operation=search e query=<x>.\n## Tool Bindings\n"
            "- `11111111-1111-1111-1111-111111111111` (Tavily)\n"
        )
        result = await dr.dry_run_tool(dr.DryRunRequest(
            skill_md=skill_md, tool_id="11111111-1111-1111-1111-111111111111",
        ))
        assert all(i.rule != "per_tool.mode_active" for i in result.issues)
