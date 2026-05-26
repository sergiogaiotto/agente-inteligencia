"""Testes do Wizard IA — geração de SKILL.md com bindings estruturados.

Cobre:
- WizardSkillRequest schema: novos campos opcionais (retrocompat).
- _infer_exec_mode: smart defaults (RAG/API → standard, senão fast).
- _build_exec_profile_yaml: shape correto pra cada mode.
- _build_wizard_prompt: monta system+user prompts com seções obrigatórias.
- _resolve_bindings_for_prompt: lookup dos IDs nos repos (mockado).
- _resolve_wizard_llm: roteamento por task_type (Wave Wizard Routing).

Mocks: pool asyncpg via AsyncMock. Não toca LLM nem Postgres real.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routes.wizard import (
    WizardAgentRequest,
    WizardRefineRequest,
    WizardSkillRequest,
    _DEFAULT_TASK_TYPE,
    _infer_exec_mode,
    _build_exec_profile_yaml,
    _build_wizard_prompt,
    _resolve_bindings_for_prompt,
    _resolve_wizard_llm,
)


# ═════════════════════════════════════════════════════════════════
# WizardSkillRequest schema — retrocompat + novos campos
# ═════════════════════════════════════════════════════════════════


class TestWizardSkillRequest:
    def test_minimal_request_works_retrocompat(self):
        """Client antigo: só description, kind, domain, provider."""
        req = WizardSkillRequest(description="skill teste")
        assert req.description == "skill teste"
        assert req.kind == "subagent"
        assert req.mcp_tool_ids == []
        assert req.source_ids == []
        assert req.table_ids == []
        assert req.api_keys == []
        assert req.exec_mode == ""

    def test_full_request_with_all_new_fields(self):
        req = WizardSkillRequest(
            description="skill teste",
            kind="router",
            domain="financeiro",
            mcp_tool_ids=["tool-1", "tool-2"],
            source_ids=["src-a"],
            table_ids=["tbl-x"],
            api_keys=["conn-1:ep-1"],
            exec_mode="rigorous",
        )
        assert req.mcp_tool_ids == ["tool-1", "tool-2"]
        assert req.source_ids == ["src-a"]
        assert req.table_ids == ["tbl-x"]
        assert req.api_keys == ["conn-1:ep-1"]
        assert req.exec_mode == "rigorous"

    def test_extra_fields_ignored_or_strict(self):
        """Pydantic ignora campos extras por default — não quebra."""
        req = WizardSkillRequest(description="x", random_field="ignored")
        assert req.description == "x"


# ═════════════════════════════════════════════════════════════════
# _infer_exec_mode — smart defaults
# ═════════════════════════════════════════════════════════════════


class TestInferExecMode:
    def test_explicit_user_choice_wins(self):
        req = WizardSkillRequest(description="x", exec_mode="rigorous", source_ids=["s1"])
        assert _infer_exec_mode(req) == "rigorous"  # respeita explicit, mesmo com RAG

    def test_rag_implies_standard(self):
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        assert _infer_exec_mode(req) == "standard"

    def test_api_implies_standard(self):
        req = WizardSkillRequest(description="x", api_keys=["c1:e1"])
        assert _infer_exec_mode(req) == "standard"

    def test_only_mcp_falls_back_to_fast(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])
        assert _infer_exec_mode(req) == "fast"

    def test_nothing_selected_falls_back_to_fast(self):
        req = WizardSkillRequest(description="x")
        assert _infer_exec_mode(req) == "fast"

    def test_rag_plus_mcp_still_standard(self):
        """RAG é o sinal mais forte — MCP adicional não derruba pra fast."""
        req = WizardSkillRequest(description="x", source_ids=["s1"], mcp_tool_ids=["t1"])
        assert _infer_exec_mode(req) == "standard"

    def test_invalid_explicit_mode_falls_back(self):
        req = WizardSkillRequest(description="x", exec_mode="ultra-rigorous-9000")
        assert _infer_exec_mode(req) == "fast"  # cai no fallback final

    def test_case_insensitive_explicit(self):
        req = WizardSkillRequest(description="x", exec_mode="RIGOROUS")
        assert _infer_exec_mode(req) == "rigorous"


# ═════════════════════════════════════════════════════════════════
# _build_exec_profile_yaml
# ═════════════════════════════════════════════════════════════════


class TestBuildExecProfileYaml:
    def test_fast_shape(self):
        yaml = _build_exec_profile_yaml("fast")
        assert "mode: fast" in yaml
        assert "reflection: off" in yaml
        assert "evidence: skip" in yaml

    def test_standard_shape(self):
        yaml = _build_exec_profile_yaml("standard")
        assert "mode: standard" in yaml
        assert "reflection: on-error" in yaml
        assert "evidence: optional" in yaml

    def test_rigorous_shape(self):
        yaml = _build_exec_profile_yaml("rigorous")
        assert "mode: rigorous" in yaml
        assert "reflection: always" in yaml
        assert "evidence: required" in yaml

    def test_unknown_mode_falls_back_to_fast(self):
        yaml = _build_exec_profile_yaml("ultra")
        assert "mode: fast" in yaml


# ═════════════════════════════════════════════════════════════════
# _build_wizard_prompt — composição do prompt
# ═════════════════════════════════════════════════════════════════


class TestBuildWizardPrompt:
    def test_returns_system_and_user_tuple(self):
        req = WizardSkillRequest(description="skill x")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, user = _build_wizard_prompt(req, bindings, "fast")
        assert user == "skill x"
        assert "SKILL.md" in system
        assert "kind: subagent" in system

    def test_includes_mcp_tools_section_when_present(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Search Tool", "description": "Busca"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "## Tool Bindings" in system
        assert "Search Tool" in system
        assert "Busca" in system

    def test_includes_rag_sources_with_human_name(self):
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "## Evidence Policy" in system
        assert "s1" in system
        assert "# Manuais (internal)" in system  # comentário humano

    def test_includes_data_tables_with_urn(self):
        req = WizardSkillRequest(description="x", table_ids=["tbl-1"])
        bindings = {
            "mcp_tools": [], "rag_sources": [],
            "data_tables": [{
                "id": "tbl-1", "name": "Vendas Q1", "urn": "urn:table:vendas:q1",
                "row_count": 1500, "schema_summary": "id:int, valor:float",
            }],
            "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "## Data Tables" in system
        assert "urn:table:vendas:q1" in system
        assert "Vendas Q1" in system
        assert "id:int" in system

    def test_includes_api_bindings_with_execution_mode_declarative(self):
        req = WizardSkillRequest(description="x", api_keys=["c1:e1"])
        bindings = {
            "mcp_tools": [], "rag_sources": [], "data_tables": [],
            "api_endpoints": [{
                "key": "c1:e1", "conn_id": "c1", "conn_name": "ERP", "ep_id": "e1",
                "ep_name": "Saldo Cliente", "method": "GET", "url": "https://erp.com/v1/saldo",
            }],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "execution_mode: declarative" in system
        assert "## API Bindings" in system
        assert "Saldo Cliente" in system

    def test_always_includes_execution_profile(self):
        req = WizardSkillRequest(description="x")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "rigorous")
        assert "## Execution Profile" in system
        assert "mode: rigorous" in system


# ═════════════════════════════════════════════════════════════════
# _resolve_bindings_for_prompt — lookup nos repositórios
# ═════════════════════════════════════════════════════════════════


def _make_pool_returning(rows_by_query: dict):
    """Mock asyncpg pool/connection.

    rows_by_query: mapeia substring SQL → lista de rows pra retornar.
    Match é por substring (case-insensitive).
    """
    con = MagicMock()

    async def _fetch(query, *args, **kwargs):
        q = query.lower()
        for sub, rows in rows_by_query.items():
            if sub.lower() in q:
                return rows
        return []

    con.fetch = AsyncMock(side_effect=_fetch)

    class _Ctx:
        async def __aenter__(self_): return con
        async def __aexit__(self_, *a): return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx())
    return pool, con


class TestResolveBindings:
    @pytest.mark.asyncio
    async def test_empty_request_returns_empty_buckets(self):
        req = WizardSkillRequest(description="x")
        result = await _resolve_bindings_for_prompt(req)
        assert result == {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}

    @pytest.mark.asyncio
    async def test_mcp_tools_lookup_resolves_names(self, monkeypatch):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1", "t2"])
        pool, _ = _make_pool_returning({
            "from tools": [
                {"id": "t1", "name": "Tavily", "description": "Web search"},
                {"id": "t2", "name": "Context7", "description": "Docs lookup"},
            ],
        })
        # Patch o _get_pool importado dentro de wizard via lazy import.
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "_get_pool", lambda: pool)

        result = await _resolve_bindings_for_prompt(req)
        assert len(result["mcp_tools"]) == 2
        assert result["mcp_tools"][0]["name"] == "Tavily"

    @pytest.mark.asyncio
    async def test_rag_sources_lookup(self, monkeypatch):
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        pool, _ = _make_pool_returning({
            "from knowledge_sources": [
                {"id": "s1", "name": "Manuais", "confidentiality_label": "internal", "kb_mode": "hybrid"},
            ],
        })
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "_get_pool", lambda: pool)

        result = await _resolve_bindings_for_prompt(req)
        assert len(result["rag_sources"]) == 1
        assert result["rag_sources"][0]["name"] == "Manuais"

    @pytest.mark.asyncio
    async def test_data_tables_summarize_schema(self, monkeypatch):
        req = WizardSkillRequest(description="x", table_ids=["tbl-1"])
        # schema_json como string JSON — função deve parsear
        pool, _ = _make_pool_returning({
            "from data_tables": [{
                "id": "tbl-1",
                "name": "Vendas",
                "urn": "urn:table:vendas:full",
                "schema_json": '{"columns": [{"name":"id","type":"int"},{"name":"valor","type":"float"}]}',
                "row_count": 100,
            }],
        })
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "_get_pool", lambda: pool)

        result = await _resolve_bindings_for_prompt(req)
        assert len(result["data_tables"]) == 1
        t = result["data_tables"][0]
        assert t["urn"] == "urn:table:vendas:full"
        assert "id:int" in t["schema_summary"]
        assert "valor:float" in t["schema_summary"]

    @pytest.mark.asyncio
    async def test_api_keys_parse_conn_ep_pairs(self, monkeypatch):
        req = WizardSkillRequest(description="x", api_keys=["c1:e1", "c1:e2", "malformed"])
        pool, _ = _make_pool_returning({
            "from api_connectors": [
                {
                    "conn_id": "c1", "conn_name": "ERP", "base_url": "https://erp.com",
                    "ep_id": "e1", "ep_name": "Saldo", "method": "GET", "path": "/saldo",
                },
                {
                    "conn_id": "c1", "conn_name": "ERP", "base_url": "https://erp.com",
                    "ep_id": "e2", "ep_name": "Cliente", "method": "POST", "path": "/cli",
                },
            ],
        })
        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "_get_pool", lambda: pool)

        result = await _resolve_bindings_for_prompt(req)
        assert len(result["api_endpoints"]) == 2
        # URLs montadas corretamente (base + path)
        urls = [ep["url"] for ep in result["api_endpoints"]]
        assert "https://erp.com/saldo" in urls
        assert "https://erp.com/cli" in urls

    @pytest.mark.asyncio
    async def test_lookup_failures_dont_break_request(self, monkeypatch):
        """Postgres offline ou erro: retorna buckets vazios pra aquela categoria,
        sem propagar exceção pro user."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])

        class FakeError(Exception):
            pass

        def _broken_pool():
            raise FakeError("postgres unreachable")

        import app.core.database as db_mod
        monkeypatch.setattr(db_mod, "_get_pool", _broken_pool)

        result = await _resolve_bindings_for_prompt(req)
        # Não levanta — só retorna vazio
        assert result["mcp_tools"] == []


# ═════════════════════════════════════════════════════════════════
# Wave Wizard Routing — _resolve_wizard_llm
# ═════════════════════════════════════════════════════════════════


class TestResolveWizardLLM:
    """Garante que os 3 wizards (skill/agent/refine) usam o roteamento
    global por task_type quando frontend manda task_type ou cai em default
    sensato por rota. Retrocompat preserva legacy provider/model explícitos."""

    def test_default_task_types_per_route(self):
        """Defaults documentados no módulo: skill→reasoning, agent→reasoning,
        refine→instruct."""
        assert _DEFAULT_TASK_TYPE["agent"] == "reasoning"
        assert _DEFAULT_TASK_TYPE["skill"] == "reasoning"
        assert _DEFAULT_TASK_TYPE["refine"] == "instruct"

    @pytest.mark.asyncio
    async def test_explicit_task_type_wins(self, monkeypatch):
        """Frontend manda task_type=reasoning → resolver usa, ignora provider legacy."""
        async def _fake_resolve(task_type, has_image=False):
            assert task_type == "reasoning"
            return ("openai", "gpt-oss-120b")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        req = WizardSkillRequest(description="x", task_type="reasoning")
        provider, model, task = await _resolve_wizard_llm(req, "skill")
        assert provider == "openai"
        assert model == "gpt-oss-120b"
        assert task == "reasoning"

    @pytest.mark.asyncio
    async def test_legacy_explicit_provider_respected(self, monkeypatch):
        """Client antigo manda provider='maritaca' (não-default) e nenhum
        task_type → respeita escolha legacy (path 2)."""
        # _resolve_wizard_llm não deve chamar resolve_llm_for_task neste caminho.
        async def _should_not_call(task_type, has_image=False):
            raise AssertionError("não deveria cair no roteador")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _should_not_call)

        req = WizardAgentRequest(description="x", provider="maritaca", model="sabia-3")
        provider, model, task = await _resolve_wizard_llm(req, "agent")
        assert provider == "maritaca"
        assert model == "sabia-3"
        assert task == ""  # legacy não retorna task_type

    @pytest.mark.asyncio
    async def test_default_provider_openai_falls_back_to_routing(self, monkeypatch):
        """provider='openai' (default antigo) E sem task_type → trata como
        'use o padrão' e cai no roteamento global (path 3 com default da rota)."""
        captured = {}
        async def _fake_resolve(task_type, has_image=False):
            captured["task_type"] = task_type
            return ("gpt-oss-120b", "openai/gpt-oss-120b")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        req = WizardSkillRequest(description="x")  # tudo default
        provider, model, task = await _resolve_wizard_llm(req, "skill")
        # Default da rota /skill é "reasoning"
        assert captured["task_type"] == "reasoning"
        assert task == "reasoning"
        assert provider == "gpt-oss-120b"

    @pytest.mark.asyncio
    async def test_refine_default_is_instruct(self, monkeypatch):
        captured = {}
        async def _fake_resolve(task_type, has_image=False):
            captured["task_type"] = task_type
            return ("gpt-oss-20b", "openai/gpt-oss-20b")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        req = WizardRefineRequest(current_content="x", instruction="melhore")
        await _resolve_wizard_llm(req, "refine")
        assert captured["task_type"] == "instruct"

    @pytest.mark.asyncio
    async def test_agent_default_is_reasoning(self, monkeypatch):
        captured = {}
        async def _fake_resolve(task_type, has_image=False):
            captured["task_type"] = task_type
            return ("gpt-oss-120b", "openai/gpt-oss-120b")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        req = WizardAgentRequest(description="x")
        await _resolve_wizard_llm(req, "agent")
        assert captured["task_type"] == "reasoning"

    @pytest.mark.asyncio
    async def test_unknown_route_falls_back_to_reasoning(self, monkeypatch):
        captured = {}
        async def _fake_resolve(task_type, has_image=False):
            captured["task_type"] = task_type
            return ("any", "model")
        monkeypatch.setattr("app.routes.wizard.resolve_llm_for_task", _fake_resolve)

        req = WizardSkillRequest(description="x")
        await _resolve_wizard_llm(req, "rota-inexistente")
        # Default global do dicionário: reasoning
        assert captured["task_type"] == "reasoning"
