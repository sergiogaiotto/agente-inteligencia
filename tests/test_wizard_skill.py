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

import re
from pathlib import Path
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

    def test_rag_without_min_relevance_omits_threshold(self):
        """Sem min_relevance no payload, o YAML do bloco obrigatório NÃO inclui
        a chave — engine aplica default 0.3.

        Verifica que a CHAVE `min_relevance: <valor>` NÃO está presente no
        bloco obrigatório. A palavra `min_relevance` pode aparecer na regra
        anti-hallucination citando o conceito — isso é OK e esperado.
        """
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Bloco obrigatório do Evidence Policy não emite a linha de YAML
        # (ex: 'min_relevance: 0.15'). Regex bate apenas se houver ': ' seguido
        # de número — não bate em prosa "use `min_relevance` configurado".
        import re as _re
        assert not _re.search(r"min_relevance:\s*\d", system)

    def test_rag_with_min_relevance_emits_threshold(self):
        """Com min_relevance setado, YAML inclui a linha — engine vai aplicar."""
        req = WizardSkillRequest(description="x", source_ids=["s1"], min_relevance=0.15)
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # YAML deve ter `min_relevance: 0.15` dentro do bloco Evidence Policy
        assert "min_relevance: 0.15" in system

    def test_min_relevance_rejects_out_of_range(self):
        """Pydantic rejeita valores fora de [0..1]."""
        import pytest as _pt
        with _pt.raises(Exception):
            WizardSkillRequest(description="x", min_relevance=1.5)
        with _pt.raises(Exception):
            WizardSkillRequest(description="x", min_relevance=-0.1)

    def test_min_relevance_accepts_extremes(self):
        """0.0 e 1.0 são valores válidos — Pydantic ge=0, le=1 (inclusive)."""
        WizardSkillRequest(description="x", min_relevance=0.0)
        WizardSkillRequest(description="x", min_relevance=1.0)

    def test_anti_halluc_rules_forbid_numeric_threshold_in_prose(self):
        """Regra 6 (2026-05-27): LLM não deve inventar valores como '0.05' em
        Workflow/Failure Modes quando user não informou.

        Bug observado: skill gerada antes desta regra tinha
        '... ≥ `min_relevance` (0.05)' no Workflow e Failure Modes em texto
        livre, mas o YAML do Evidence Policy não tinha a chave. Engine usava
        default 0.30 enquanto a 'documentação' da skill dizia 0.05.
        """
        req = WizardSkillRequest(description="x", source_ids=["s1"])  # sem min_relevance
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # System prompt deve EXPLICITAMENTE proibir inventar valores numéricos
        assert "NÃO invente valores numéricos" in system
        # Deve dar exemplos concretos de como o LLM pode citar sem número
        assert "score abaixo do `min_relevance`" in system or "threshold definido em Evidence Policy" in system
        # Deve instruir explicitamente: sem bloco obrigatório → não cite número
        assert "NÃO cite número nenhum" in system

    def test_anti_halluc_rule_7_reinforces_exact_value_when_provided(self):
        """Quando user FORNECE min_relevance, system prompt instrui LLM a usar
        EXATAMENTE esse número se mencionar em prosa — evita drift entre o
        valor declarado no YAML e o citado no Workflow."""
        req = WizardSkillRequest(description="x", source_ids=["s1"], min_relevance=0.15)
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Regra 7 ativada — cita o valor exato
        assert "0.15" in system
        assert "EXATAMENTE esse número" in system

    def test_no_threshold_rule_7_when_no_min_relevance(self):
        """Sem min_relevance, regra 7 não aparece (não há valor pra reforçar)."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Texto da regra 7 só aparece quando há valor a reforçar
        assert "EXATAMENTE esse número" not in system

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

    def test_always_includes_anti_hallucination_rules(self):
        """Regra crítica do system_prompt: pra qualquer combinação de bindings,
        o LLM precisa receber as REGRAS ANTI-INVENÇÃO explícitas. Bug user
        2026-05-27: escolheu só RAG e Wizard gerou `knowledge_search`/
        `summarize_text` inventadas em ## Tool Bindings."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "ANTI-INVENÇÃO" in system, (
            "Regras anti-hallucination devem estar SEMPRE no system_prompt"
        )
        # Cita exemplos concretos das tools que o LLM costuma inventar
        assert "knowledge_search" in system or "NÃO invente" in system

    def test_tool_bindings_explicit_when_no_mcp_with_rag(self):
        """Bug user 2026-05-27: escolheu só RAG, sem MCP. Antes deste fix
        o system_prompt deixava `## Tool Bindings` sem orientação no bloco
        obrigatório → LLM completava com knowledge_search/summarize_text.
        Agora a seção é incluída EXPLICITAMENTE com declaração de vazio
        + menção dos recursos reais disponíveis (RAG nesse caso)."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Tool Bindings está no system, MAS com texto declarativo de vazio
        # (não com lista de tools inventadas).
        assert "## Tool Bindings" in system
        # A frase exata sinaliza ao LLM pra não inventar
        assert "não usa ferramentas MCP" in system or "NÃO invente" in system
        # Cita o recurso real (RAG) que está disponível, pra o LLM não se
        # sentir "obrigado" a inventar tools.
        assert "RAG" in system

    def test_tool_bindings_explicit_when_no_bindings_at_all(self):
        """Cenário extremo: skill pura de raciocínio, sem MCP, sem RAG,
        sem tabelas, sem APIs. Tool Bindings ainda deve ser declarada
        explicitamente para o LLM não preencher do nada."""
        req = WizardSkillRequest(description="x")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "## Tool Bindings" in system
        # Declara o caso "apenas raciocínio LLM"
        assert "raciocínio" in system.lower() or "sem bindings" in system.lower()

    def test_mcp_section_intact_when_mcp_selected(self):
        """Não-regressão: quando user seleciona MCP, a lista real é injetada
        normalmente (sem o stub de vazio)."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Search Tool", "description": "Busca"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "Search Tool" in system
        # NÃO deve aparecer a frase de "skill não usa ferramentas MCP"
        # quando MCP está presente.
        assert "não usa ferramentas MCP" not in system

    def test_never_includes_budget_section_in_prompt(self):
        """User reportou: seções Budget geradas automaticamente prejudicam
        desempenho em runtime (tokens=2000, latência=4s, custo=$0.0015).
        Operador deve definir budget conscientemente depois. Wizard NÃO
        deve sugerir Budget como parte da estrutura canônica nem dar
        valores padrão."""
        req = WizardSkillRequest(description="x")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Não pode aparecer como HEADER no template canônico (linha começando
        # com "## Budget" seguida de descrição). A menção dentro da instrução
        # negativa ("NÃO inclua `## Budget`") é OK e desejada.
        canonical_header = "## Budget\nLimites de tokens"
        assert canonical_header not in system
        # Instrução negativa explícita deve orientar o LLM
        assert "NÃO inclua a seção" in system
        assert "Budget" in system  # menção da palavra na instrução negativa OK


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
        """Defaults documentados no módulo: skill→skill_generation (desde
        2026-05-29 — separado de reasoning após gpt-oss-120b errar 4x),
        agent→reasoning, refine→instruct."""
        assert _DEFAULT_TASK_TYPE["agent"] == "reasoning"
        assert _DEFAULT_TASK_TYPE["skill"] == "skill_generation"
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
        # Default da rota /skill é "skill_generation" (separado de reasoning
        # em 2026-05-29 — bugs Context7 #1-#4 mostraram que gpt-oss-120b vinha
        # falhando consistentemente).
        assert captured["task_type"] == "skill_generation"
        assert task == "skill_generation"
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


# ═════════════════════════════════════════════════════════════════
# Esforço de raciocínio configurável (27.0.0) — _wizard_reasoning_effort
# ═════════════════════════════════════════════════════════════════
#
# Antes era a constante hardcoded _WIZARD_REASONING_EFFORT="high"; agora vem do
# setting wizard_reasoning_effort (aba Parâmetros). O gate por MODELO continua em
# get_provider — este helper só LÊ e SANITIZA o valor.
class TestWizardReasoningEffort:
    @pytest.mark.parametrize("value,expected", [
        ("high", "high"),
        ("medium", "medium"),
        ("low", "low"),
        ("HIGH", "high"),        # case-insensitive
        ("  high  ", "high"),    # trim
        ("", None),               # vazio = desligado
        ("off", None),            # valor inválido = desligado
        ("altíssimo", None),      # sanitização de lixo
        (None, None),             # ausência = desligado
    ])
    def test_reads_and_sanitizes_setting(self, monkeypatch, value, expected):
        from app.routes import wizard as _wiz

        class _Stub:
            wizard_reasoning_effort = value

        monkeypatch.setattr(_wiz, "get_settings", lambda: _Stub())
        assert _wiz._wizard_reasoning_effort() == expected

    def test_default_setting_is_high(self):
        """O default do setting preserva o comportamento anterior (high).
        Testa o default da CLASSE (hermético) — Settings() leria os.environ e
        o .env do host, e quebraria sem regressão de código no dia em que
        alguém setasse WIZARD_REASONING_EFFORT localmente."""
        from app.core.config import Settings
        assert Settings.model_fields["wizard_reasoning_effort"].default == "high"


# ═════════════════════════════════════════════════════════════════
# Verbosidade da geração (68.0.0) — _wizard_verbosity + níveis do prompt
# ═════════════════════════════════════════════════════════════════
#
# O nível controla o tamanho do DOCUMENTO gerado (não da resposta em runtime).
# Só o corpo do esqueleto varia; a armadura anti-alucinação (regras 1-7,
# G1-G4, bloco obrigatório, Execution Profile) é idêntica nos 3 níveis.

_EMPTY_BINDINGS = {
    "mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": [],
}
_FIXTURES = Path(__file__).parent / "fixtures"


def _prompt(verbosity, bindings=None, **req_kwargs):
    req = WizardSkillRequest(
        description=req_kwargs.pop("description", "Skill de teste para snapshot de prompt."),
        kind="subagent", domain="qa", **req_kwargs,
    )
    system, _ = _build_wizard_prompt(
        req, bindings or _EMPTY_BINDINGS, "standard", verbosity=verbosity,
    )
    return system


class TestWizardVerbositySetting:
    @pytest.mark.parametrize("value,expected", [
        ("enxuto", "enxuto"),
        ("padrao", "padrao"),
        ("didatico", "didatico"),
        ("DIDATICO", "didatico"),     # case-insensitive
        ("  enxuto  ", "enxuto"),     # trim
        ("", "didatico"),             # vazio = default (comportamento anterior)
        ("médio", "didatico"),        # lixo (env crua) = default, fail-safe
        (None, "didatico"),           # ausência = default
    ])
    def test_reads_and_sanitizes_setting(self, monkeypatch, value, expected):
        from app.routes import wizard as _wiz

        class _Stub:
            wizard_verbosity = value

        monkeypatch.setattr(_wiz, "get_settings", lambda: _Stub())
        assert _wiz._wizard_verbosity() == expected

    def test_default_setting_is_didatico(self):
        """Default 'didatico' = prompt byte-idêntico ao pré-68.0.0.
        Default da CLASSE (hermético — não lê os.environ nem o .env do host)."""
        from app.core.config import Settings
        assert Settings.model_fields["wizard_verbosity"].default == "didatico"


class TestWizardVerbosityPrompt:
    def test_didatico_byte_identico_ao_golden_sem_bindings(self):
        """REGRESSÃO ZERO no default: o prompt em 'didatico' é byte-idêntico
        ao capturado ANTES do refactor (fixture gravada na 67.2.0)."""
        golden = (_FIXTURES / "wizard_prompt_golden_didatico_sem_bindings.txt").read_text(encoding="utf-8")
        assert _prompt("didatico") == golden

    def test_didatico_byte_identico_ao_golden_decisions(self):
        req = WizardSkillRequest(
            description="Skill de triagem com contrato de decisão.",
            kind="subagent", domain="qa",
            min_relevance=0.25, length_preset="digest",
            decisions={"prioridade": ["alta", "baixa"]},
        )
        system, _ = _build_wizard_prompt(req, _EMPTY_BINDINGS, "rigorous", verbosity="didatico")
        golden = (_FIXTURES / "wizard_prompt_golden_didatico_decisions.txt").read_text(encoding="utf-8")
        assert system == golden

    def test_sem_verbosity_explicito_le_o_setting(self, monkeypatch):
        """verbosity=None (caminho do endpoint hoje) → setting da plataforma."""
        from app.routes import wizard as _wiz

        class _Stub:
            wizard_verbosity = "enxuto"

        monkeypatch.setattr(_wiz, "get_settings", lambda: _Stub())
        req = WizardSkillRequest(description="x")
        system, _ = _build_wizard_prompt(req, _EMPTY_BINDINGS, "fast")
        assert "REGRAS DE CONCISÃO (nível ENXUTO — CRÍTICAS)" in system

    def test_verbosity_explicito_sobrepoe_setting(self, monkeypatch):
        from app.routes import wizard as _wiz

        class _Stub:
            wizard_verbosity = "enxuto"

        monkeypatch.setattr(_wiz, "get_settings", lambda: _Stub())
        golden = (_FIXTURES / "wizard_prompt_golden_didatico_sem_bindings.txt").read_text(encoding="utf-8")
        assert _prompt("didatico") == golden

    def test_armadura_identica_nos_tres_niveis(self):
        """Regras anti-invenção + Execution Profile + bloco obrigatório não
        variam com o nível — só o corpo do esqueleto varia."""
        for level in ("enxuto", "padrao", "didatico"):
            system = _prompt(level)
            assert "REGRAS ANTI-INVENÇÃO (CRÍTICAS)" in system, level
            assert "## Execution Profile" in system, level
            assert "SEÇÕES OBRIGATÓRIAS A INCLUIR NO SKILL.md" in system, level
            # placeholder de Tool Bindings (skill sem MCP não inventa tools)
            assert "NÃO invente nomes de tools MCP" in system, level

    def test_fios_de_alta_tensao_no_enxuto_com_mcp(self):
        """G1 (verbo imperativo) e o passo 1 com `operation=` sobrevivem ao
        nível enxuto — sem eles o wizard_validator dispararia retry."""
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Search Tool",
                           "description": "Busca", "operations": "docs, code"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system = _prompt("enxuto", bindings=bindings, mcp_tool_ids=["t1"])
        assert "VERBO IMPERATIVO" in system            # G1 intacta
        assert "operation=docs" in system              # passo 1 injetado
        assert "REGRAS DE CONCISÃO (nível ENXUTO — CRÍTICAS)" in system

    def test_enxuto_um_exemplo_e_sem_evidence_policy_no_esqueleto(self):
        system = _prompt("enxuto")
        assert "UM único exemplo" in system
        # Evidence Policy sai do ESQUELETO no enxuto — o teste ancora na linha
        # descritiva da seção, porque "## Evidence Policy" também aparece
        # (corretamente) na regra anti-invenção nº 3, armadura presente em
        # TODOS os níveis. Com RAG o bloco obrigatório injeta o YAML real; em
        # skill só-MCP o sub-bloco exige a versão de UMA linha (carve-out
        # testado abaixo).
        assert "Bases autorizadas e thresholds de evidência" not in system
        assert "Seja específico e enxuto." in system
        assert "Seja específico e detalhado." not in system

    def test_enxuto_com_rag_mantem_evidence_policy_do_bloco_obrigatorio(self):
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais",
                             "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system = _prompt("enxuto", bindings=bindings, source_ids=["s1"])
        assert "## Evidence Policy" in system   # YAML real, do bloco obrigatório

    def test_enxuto_so_mcp_preserva_carve_out_de_evidence_policy(self):
        """Skill só-MCP: o sub-bloco MCP (armadura comum aos 3 níveis) manda
        escrever a Evidence Policy de UMA linha ("única fonte autorizada é o
        binding") — a proibição de seções extras do enxuto abre exceção
        explícita para os sub-blocos, senão o MESMO prompt mandaria e
        proibiria a seção ao mesmo tempo (saída não-determinística)."""
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Search Tool",
                           "description": "Busca", "operations": "docs, code"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system = _prompt("enxuto", bindings=bindings, mcp_tool_ids=["t1"])
        assert "a seção Evidence Policy deve dizer" in system  # ordem do sub-bloco MCP
        assert "das exigidas explicitamente pelos SUB-BLOCOS" in system  # carve-out

    def test_padrao_limita_exemplos_e_tem_contrapeso(self):
        system = _prompt("padrao")
        assert "NO MÁXIMO 2 exemplos" in system
        assert "REGRAS DE CONCISÃO (nível PADRÃO)" in system
        assert "Seja específico e detalhado." not in system

    def test_niveis_concisos_protegem_declarative_inputs_e_evidence(self):
        """Endurecimento pós-auditoria (68.2.0): a lista nunca-encurte dos
        níveis concisos cita EXPLICITAMENTE os 3 riscos que a compressão
        amplificava — `execution_mode: declarative` do frontmatter (Tabela/API
        morrem em silêncio sem ele), o inputs_schema multi-coluna exigido pelo
        sub-bloco de Tabelas, e o formato COMPLETO da Evidence Policy (a
        versão de UMA linha vale só para skill só-MCP)."""
        enxuto = _prompt("enxuto")
        assert "`execution_mode: declarative` quando presente" in enxuto
        assert "TODAS as propriedades que o sub-bloco de Tabelas exigir" in enxuto
        assert "a versão de UMA linha vale SÓ para skill só-MCP" in enxuto
        padrao = _prompt("padrao")
        # (frase quebra de linha no corpo padrão — asserts não atravessam o wrap)
        assert "incluindo a diretiva de frontmatter `execution_mode: declarative`" in padrao
        assert "`inputs_schema` que o sub-bloco de Tabelas exigir" in padrao


# ═════════════════════════════════════════════════════════════════
# Matriz bindings × verbosidade (68.1.1) — nenhum nível degrada binding
# ═════════════════════════════════════════════════════════════════
#
# Pergunta do usuário selada em teste: API/RAG/Tabela/MCP/Decisões continuam
# corretamente configurados na skill gerada em QUALQUER nível? O que configura
# os bindings é o bloco SEÇÕES OBRIGATÓRIAS (+ regras/sub-blocos) — este teste
# prova, por cenário, que ele é BYTE-IDÊNTICO nos 3 níveis e que os marcadores
# de configuração de cada tipo estão presentes. Nível novo ou corpo editado
# que vaze para o bloco obrigatório fica vermelho aqui.

_MX_MCP = {"id": "t1", "name": "Search Tool", "description": "Busca docs",
           "operations": "docs, code"}
_MX_RAG = {"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}
_MX_TAB = {"urn": "urn:table:clientes", "name": "clientes", "row_count": 100,
           "schema_summary": "cd_cliente BIGINT, uf VARCHAR",
           "columns": ["cd_cliente", "uf"], "suggested_pk": "cd_cliente"}
_MX_API = {"ep_id": "ep1", "conn_id": "c1", "ep_name": "Consulta CEP",
           "method": "GET", "path": "/cep/{cep}", "url": "https://api.exemplo/cep"}

_MX_CENARIOS = {
    "mcp": (
        {"mcp_tools": [_MX_MCP], "rag_sources": [], "data_tables": [], "api_endpoints": []},
        {"mcp_tool_ids": ["t1"]},
        ["`t1` (Search Tool)", "Operations declaradas", "operation=docs"],
    ),
    "rag": (
        {"mcp_tools": [], "rag_sources": [_MX_RAG], "data_tables": [], "api_endpoints": []},
        {"source_ids": ["s1"], "min_relevance": 0.2},
        ["## Evidence Policy", "- s1   # Manuais (internal)", "min_relevance: 0.2"],
    ),
    "tabela": (
        {"mcp_tools": [], "rag_sources": [], "data_tables": [_MX_TAB], "api_endpoints": []},
        {"table_ids": ["tb1"]},
        ["execution_mode: declarative", "table_ref: urn:table:clientes",
         "if_present: cd_cliente", "output_mapping"],
    ),
    "api": (
        {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": [_MX_API]},
        {"api_keys": ["c1:ep1"]},
        ["## API Bindings", "connector: c1", "path: /cep/{cep}",
         "execution_mode: declarative"],
    ),
    "decisoes": (
        {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []},
        {"decisions": {"categoria": ["tecnico", "financeiro"]}},
        ["## Decisions", '"categoria"', '"tecnico"'],
    ),
    "combinado": (
        {"mcp_tools": [_MX_MCP], "rag_sources": [_MX_RAG],
         "data_tables": [_MX_TAB], "api_endpoints": [_MX_API]},
        {"mcp_tool_ids": ["t1"], "source_ids": ["s1"], "table_ids": ["tb1"],
         "api_keys": ["c1:ep1"], "decisions": {"categoria": ["tecnico", "financeiro"]},
         "min_relevance": 0.15, "length_preset": "digest"},
        ["`t1` (Search Tool)", "## Evidence Policy", "table_ref: urn:table:clientes",
         "## API Bindings", "## Decisions", "length_preset: digest"],
    ),
}

_MX_OBRIG_INI = "=== SEÇÕES OBRIGATÓRIAS A INCLUIR NO SKILL.md ==="
_MX_OBRIG_FIM = "=== FIM DAS SEÇÕES OBRIGATÓRIAS ==="
_MX_SKELETON_MARK = "O SKILL.md deve conter EXATAMENTE"


class TestWizardVerbosityBindingsMatrix:
    @pytest.mark.parametrize("cenario", sorted(_MX_CENARIOS))
    def test_bloco_obrigatorio_e_regras_identicos_nos_3_niveis(self, cenario):
        from app.routes.wizard import _WIZARD_VERBOSITY_VALUES
        bindings, req_kwargs, marcadores = _MX_CENARIOS[cenario]
        req = WizardSkillRequest(description="skill de teste", kind="subagent",
                                 domain="qa", **req_kwargs)
        blocos, regras = {}, {}
        for level in sorted(_WIZARD_VERBOSITY_VALUES):
            sp, _ = _build_wizard_prompt(req, bindings, "standard", verbosity=level)
            blocos[level] = sp[sp.index(_MX_OBRIG_INI):sp.index(_MX_OBRIG_FIM)]
            # tudo ANTES do esqueleto = anti-invenção + G1-G4 + sub-blocos
            regras[level] = sp[:sp.index(_MX_SKELETON_MARK)]
        assert len(set(blocos.values())) == 1, (
            f"{cenario}: bloco SEÇÕES OBRIGATÓRIAS DIVERGE entre níveis — "
            "verbosidade jamais pode tocar a configuração dos bindings")
        assert len(set(regras.values())) == 1, (
            f"{cenario}: regras de binding (G1-G4/anti-invenção) divergem entre níveis")
        for m in marcadores:
            assert m in next(iter(blocos.values())), (
                f"{cenario}: marcador de configuração ausente do bloco obrigatório: {m!r}")


# ═════════════════════════════════════════════════════════════════
# Verbosidade por GERAÇÃO (68.1.0) — campo do request + modal
# ═════════════════════════════════════════════════════════════════


class TestWizardVerbosityRequestField:
    def test_default_none_e_valores_validos(self):
        """Itera o frozenset de RUNTIME (não uma tupla literal): se o enum
        crescer sem o pattern do request acompanhar, este teste fica vermelho
        — senão a modal enviaria o nível novo e a geração morreria em 422."""
        from app.routes.wizard import _WIZARD_VERBOSITY_VALUES
        assert WizardSkillRequest(description="x").verbosity is None
        for v in sorted(_WIZARD_VERBOSITY_VALUES):
            assert WizardSkillRequest(description="x", verbosity=v).verbosity == v

    def test_valor_invalido_e_422(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="verbosity"):
            WizardSkillRequest(description="x", verbosity="máximo")

    def test_resolve_verbosity_explicito_vence_setting(self, monkeypatch):
        """Único ponto de resolução (prompt E `resolved` da resposta usam o
        mesmo) — override por geração vence; None cai no setting."""
        from app.routes import wizard as _wiz

        # Stub ≠ fail-safe 'didatico' DE PROPÓSITO: com 'didatico' a asserção
        # do ramo None passaria mesmo se o código ignorasse o setting e
        # devolvesse a constante default (mutação invisível).
        class _Stub:
            wizard_verbosity = "padrao"

        monkeypatch.setattr(_wiz, "get_settings", lambda: _Stub())
        assert _wiz._resolve_verbosity("enxuto") == "enxuto"
        assert _wiz._resolve_verbosity(None) == "padrao"


class TestWizardVerbosityModalUi:
    """Invariantes de template da modal do Wizard: cards presentes, payload
    envia o campo, toast reporta o nível efetivo — e o enum dos cards ≡ enum
    de runtime (mesma classe #721 selada na aba Parâmetros)."""

    def test_cards_payload_e_toast(self):
        src = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert 'data-testid="wizard-verbosity"' in src
        for v in ("enxuto", "padrao", "didatico"):
            assert f'data-testid="wizard-verbosity-{v}"' in src
        assert "wizardVerbosity: ''" in src                      # estado (vazio = setting)
        assert "verbosity: this.wizardVerbosity || null" in src  # payload da geração
        # Toast: o fragmento existe E está concatenado no return — só a
        # substring 'resolved.verbosity' deixaria verde a mutação de remover
        # `+ verbosity` do return mantendo a const órfã.
        assert "SKILL ${resolved.verbosity}" in src
        assert "return bindings + profile + verbosity + llm" in src

    def test_enum_dos_cards_bate_com_runtime(self):
        from app.routes.wizard import _WIZARD_VERBOSITY_VALUES
        src = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        # Harvest 1 — literais de toggle: [^']+ captura QUALQUER value (acento,
        # hífen, maiúscula, dígito), então um 4º card com typo entra no set e
        # quebra a igualdade, em vez de escapar do regex ([a-z]+ era fail-open).
        toggles = set(re.findall(r"wizardVerbosity === '([^']+)'", src))
        assert toggles == set(_WIZARD_VERBOSITY_VALUES)
        # Harvest 2 (independente) — testids dos cards: pega card escrito sem
        # espaços no === ou refatorado para helper, que escaparia do harvest 1.
        testids = set(re.findall(r'data-testid="wizard-verbosity-([^"]+)"', src))
        assert testids == set(_WIZARD_VERBOSITY_VALUES)


# ═════════════════════════════════════════════════════════════════
# Regras de invocação MCP — fix bug Context7 (2026-05-29)
# ═════════════════════════════════════════════════════════════════
#
# Bug observado: Wizard gerou SKILL.md "Design Pattern Generator for Context 7"
# com Workflow passivo ("enriquecimento com Context 7 usando o binding") +
# Examples sem rastro de tool call + Evidence Policy ambígua ("nenhuma fonte
# externa autorizada" contradizendo "informação provém do binding"). Em
# runtime, gpt-oss-120b leu o conjunto como autorização pra responder de
# cabeça e ignorou silenciosamente o tool_choice forçado.
#
# Fix em _build_wizard_prompt: novo bloco mcp_invocation_rules emitido SÓ
# quando há tools MCP no bindings — instrui o LLM gerador a usar verbo
# imperativo no Workflow, mostrar tool call nos Examples, e escrever
# Evidence Policy coerente quando só há MCP (sem RAG).


class TestMCPInvocationRules:
    """Regras condicionais quando o bloco obrigatório tem MCP tools.

    Garantia: nenhuma dessas regras aparece quando bindings["mcp_tools"]
    é vazio — back-compat com skills sem MCP é preservada.
    """

    def _bindings_with_mcp(self, name: str = "Context 7 MCP Server",
                           operations: str = "docs,code,prompt",
                           description: str = ""):
        return {
            "mcp_tools": [{
                "id": "tool-id-1",
                "name": name,
                "description": description or "Plataforma para documentação atualizada",
                "operations": operations,
            }],
            "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [],
        }

    def test_mcp_description_not_truncated_at_100_chars(self):
        """Bug literal do user: descrição cortou em 'MCP Se' (100 chars).
        Fix: truncamento agora é 300, alinhado com build_openai_tools e
        engine._build_system_prompt."""
        long_desc = (
            "Plataforma Context7 para documentação e código atualizado de "
            "qualquer prompt, disponível como MCP Server com operações docs/"
            "code/prompt para enriquecer respostas com dados frescos da fonte."
        )
        assert len(long_desc) > 100  # sanity: a descrição original > 100
        assert len(long_desc) < 300   # mas < 300, então não trunca no fix
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(description=long_desc)
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # A descrição completa precisa aparecer no bloco obrigatório
        assert long_desc in system, (
            "Descrição truncou — palavra final do bug original era 'fonte', "
            "antes era 'MCP Se' (100 chars). Truncamento mudou pra 300."
        )

    def test_mcp_description_still_truncates_at_hard_limit(self):
        """300 chars é o hard limit — descrições insanas ainda truncam pra
        não inflar o prompt indefinidamente."""
        huge = "A" * 500
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(description=huge)
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # 300 As contínuos aparecem
        assert "A" * 300 in system
        # 301 As contínuos NÃO aparecem (truncou)
        assert "A" * 301 not in system

    def test_mcp_rules_block_present_when_tools_declared(self):
        """Cabeçalho geral + sub-bloco [MCP] devem estar presentes."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp()
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system
        assert "[MCP]" in system

    def test_binding_rules_block_absent_when_no_bindings(self):
        """Back-compat: skill puramente de raciocínio (sem nenhum binding)
        NÃO recebe o bloco — evita poluir prompt em casos simples.
        Skill com QUALQUER binding (incluindo RAG só) recebe."""
        req = WizardSkillRequest(description="x")
        bindings = {
            "mcp_tools": [], "rag_sources": [],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" not in system

    def test_mcp_rule_A_demands_imperative_verb(self):
        """Workflow precisa ter verbo imperativo (Chame/Consulte/etc).
        Verbos passivos foram a causa do bug Context7."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp()
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Lista de verbos aceitos precisa estar visível
        assert "Chame" in system
        assert "Consulte" in system
        # Lista de verbos REJEITADOS precisa estar visível (com o exato
        # vocabulário que apareceu no bug — "enriquecimento", "usando o binding")
        assert "enriquecimento" in system
        assert "usando o binding" in system
        # E precisa marcar como INSUFICIENTE pra ser claro
        assert "INSUFICIENTES" in system

    def test_mcp_rule_B_forbids_internal_template_phrases(self):
        """Frases proibidas no Workflow: 'template interno', 'recursos internos'.
        Eram a causa principal de gpt-oss-120b ignorar a tool no bug Context7."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp()
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "template interno" in system
        assert "recursos internos" in system
        # Marcadas como proibidas
        assert "NÃO use" in system or "NÃO escreva" in system

    def test_mcp_rule_C_demands_tool_call_in_examples(self):
        """Examples DEVE rastrear tool call antes do output final.
        Padrão G3 (geral) + exemplo concreto [MCP]."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp()
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Padrão G3 geral
        assert "Saída final" in system
        # Sub-bloco MCP cita "Chamada à tool"
        assert "Chamada à tool" in system
        # Aviso explícito contra pular pra saída direto
        assert "alucinar" in system.lower()

    def test_mcp_rule_evidence_policy_text_when_no_rag(self):
        """Quando só há MCP (sem RAG), Evidence Policy deve dizer
        explicitamente 'única fonte autorizada é o binding X'.
        Texto agora vive no sub-bloco [MCP] (não mais como regra D nomeada)."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(name="Context 7 MCP Server")
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Texto obrigatório pra Evidence Policy quando só há MCP
        assert "única fonte autorizada é o binding" in system
        assert "Context 7 MCP Server" in system

    def test_mcp_rule_uses_actual_tool_name_in_example(self):
        """O exemplo de Workflow precisa usar o nome EXATO da tool, não
        placeholder genérico — Wizard tem que fazer string interp."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(name="Context 7 MCP Server")
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Nome literal aparece dentro de backticks no exemplo
        assert "`Context 7 MCP Server`" in system

    def test_mcp_rule_uses_first_operation_as_hint(self):
        """Wizard pega a primeira operation como hint pro exemplo. Confirma
        que a operation chega lá."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(operations="docs,code,prompt")
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # 'docs' é a primeira operation → vai pro hint
        assert "operation=docs" in system or "operation=`docs`" in system

    def test_mcp_rule_falls_back_to_generic_op_when_no_operations(self):
        """Tool sem operations declaradas — exemplo usa fallback 'search'."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp(operations="")
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "operation=search" in system or "operation=`search`" in system

    def test_examples_template_warns_about_binding_interaction(self):
        """Template do ## Examples deve avisar do tool call/binding
        interaction quando há QUALQUER binding presente."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._bindings_with_mcp()
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Frase aparece em 2 lugares (template canônico + sub-bloco MCP)
        import re as _re
        assert _re.search(r"rastrear\s+(a\s+interação\s+com\s+o\s+binding|a\s+chamada\s+da|o\s+tool\s+call)", system)

    def test_examples_template_aviso_é_geral_pra_qualquer_binding(self):
        """O aviso no template ## Examples cita 'QUALQUER binding' (não só
        MCP) — vale igual pra RAG/API/Tabelas. Skill com RAG só também
        recebe o aviso e respeita o padrão Entrada → Ação → Resposta → Saída."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "Manuais", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Aviso GERAL aparece (não específico de MCP)
        assert "QUALQUER binding" in system
        # E o sub-bloco [RAG] também é ativado pra essa skill
        assert "[RAG]" in system

    def test_multiple_tools_all_names_listed(self):
        """Skill com 2+ tools: prompt cita todas no header das regras."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1", "t2"])
        bindings = {
            "mcp_tools": [
                {"id": "t1", "name": "Tool Alpha", "description": "A", "operations": "search"},
                {"id": "t2", "name": "Tool Beta",  "description": "B", "operations": "fetch"},
            ],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        assert "`Tool Alpha`" in system
        assert "`Tool Beta`" in system

    def test_mcp_plus_rag_keeps_both_blocks(self):
        """Skill mista (MCP + RAG): header geral + ambos sub-blocos
        ([MCP] e [RAG]) são emitidos. E Evidence Policy do RAG segue
        aparecendo no obligatory_block. Não há conflito — paths
        independentes."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"], source_ids=["s1"])
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Tool X", "description": "Y", "operations": "search"}],
            "rag_sources": [{"id": "s1", "name": "Bases", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system
        # Ambos sub-blocos ativos
        assert "[MCP]" in system
        assert "[RAG]" in system
        # Evidence Policy do RAG segue aparecendo no obligatory_block
        assert "## Evidence Policy" in system
        assert "s1" in system


class TestRegressionContext7Bug:
    """Regressão direta do caso real reportado pelo user (2026-05-29).

    SKILL gerada pelo Wizard pro "Design Pattern Generator for Context 7"
    tinha 4 problemas que faziam gpt-oss-120b ignorar a tool em runtime.
    Estes testes garantem que o prompt do Wizard NÃO produz mais SKILL
    com esses gaps quando há MCP tool declarada.
    """

    def _ctx7_bindings(self):
        return {
            "mcp_tools": [{
                "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
                "name": "Context 7 MCP Server",
                "description": "Plataforma Context7 para documentação e código atualizado de qualquer prompt, disponível como MCP Server",
                "operations": "docs,code,prompt",
            }],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }

    def test_context7_full_description_visible_in_prompt(self):
        """Bug literal: 'MCP Se' truncado. Fix: 300 chars."""
        req = WizardSkillRequest(
            description="design pattern generator context 7",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # A frase final que ANTES truncava deve aparecer inteira
        assert "MCP Server" in system, (
            "Descrição da tool ainda trunca antes do final — "
            "o bug do user era exatamente 'MCP Se' cortado a 100 chars."
        )

    def test_context7_prompt_blocks_passive_workflow(self):
        """SKILL gerada tinha 'Enriquecimento com Context 7 — incorpora
        informações... usando o binding'. Wizard agora marca esses verbos
        como insuficientes."""
        req = WizardSkillRequest(
            description="design pattern generator context 7",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # Estas 2 palavras exatas estavam no SKILL ruim — agora precisam
        # aparecer como AVISO no prompt do Wizard
        assert "incorpora" in system or "usando o binding" in system
        # E perto de "INSUFICIENTES" pra o LLM gerador não usar
        idx_insuf = system.find("INSUFICIENTES")
        idx_passive = max(system.find("incorpora"), system.find("usando o binding"))
        assert idx_insuf > 0 and idx_passive > 0
        # As 2 menções ficam num raio de 500 chars pra serem percebidas
        # como bloco coerente, não citações desconexas
        assert abs(idx_insuf - idx_passive) < 500

    def test_context7_prompt_warns_against_nenhuma_fonte_externa(self):
        """A SKILL ruim tinha 'Nenhuma fonte de conhecimento externa está
        autorizada' — frase exata. Regra G4 generaliza pra qualquer binding
        e cita variantes dessa frase como proibidas."""
        req = WizardSkillRequest(
            description="design pattern generator context 7",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        low = system.lower()
        # Regra G4 cita a frase em minúsculo + variantes
        assert "nenhuma fonte externa autorizada" in low
        # Precisa estar no contexto de NUNCA escrever (G4)
        idx_nunca = system.find("NUNCA escreva")
        idx_frase = low.find("nenhuma fonte externa")
        assert idx_nunca > 0 and idx_frase > 0
        assert abs(idx_nunca - idx_frase) < 300


class TestWorkflowPreInjection:
    """Pre-injection (2026-05-29 PR #192): em 4 tentativas consecutivas
    o LLM gerador (gpt-oss-120b) omitiu `operation=` no Workflow. Validador
    detectava e fazia retry, mas o LLM continuava errando.

    Fix: Wizard injeta LITERALMENTE o passo 1 do Workflow em
    obligatory_sections com a primeira operation declarada. LLM só escreve
    os passos 2-N.
    """

    def _mcp_only_bindings(self, ops="docs,code,prompt"):
        return {
            "mcp_tools": [{
                "id": "tool-id-1", "name": "Context 7 MCP Server",
                "description": "Plataforma Context7",
                "operations": ops,
            }],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }

    def test_workflow_section_pre_injected_when_mcp_has_operations(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        system, _ = _build_wizard_prompt(req, self._mcp_only_bindings(), "fast")
        # `## Workflow` aparece no obligatory_block (não só no template canônico)
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        assert "## Workflow" in obligatory

    def test_pre_injected_workflow_uses_first_operation(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        system, _ = _build_wizard_prompt(req, self._mcp_only_bindings(), "fast")
        # Procura "operation=docs" literal no obligatory
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        assert "operation=docs" in obligatory
        assert "`Context 7 MCP Server`" in obligatory

    def test_pre_injected_workflow_uses_imperative_chame(self):
        """Verbo IMPERATIVO no passo 1 — não 'enriquecimento' etc."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        system, _ = _build_wizard_prompt(req, self._mcp_only_bindings(), "fast")
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        assert "**Chame**" in obligatory

    def test_pre_injected_workflow_instructs_literal_preservation(self):
        """Texto explica ao LLM que NÃO pode alterar o passo 1."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        system, _ = _build_wizard_prompt(req, self._mcp_only_bindings(), "fast")
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        # Instrução de preservação literal
        assert "LITERAL" in obligatory
        # E orienta o LLM a adicionar passos 2-N
        assert "passos 2-N" in obligatory or "Passos 2-N" in obligatory

    def test_no_pre_injection_when_no_mcp_tool(self):
        """Skill sem MCP — Workflow é responsabilidade do LLM (back-compat)."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [],
            "rag_sources": [{"id": "s1", "name": "M", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        assert "## Workflow" not in obligatory

    def test_no_pre_injection_when_mcp_without_operations(self):
        """Tool MCP sem operations no Registry — não tem como pre-injetar
        operation= literal. Cai no path antigo (LLM gerador escreve)."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        bindings = self._mcp_only_bindings(ops="")
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        assert "## Workflow" not in obligatory

    def test_pre_injection_uses_first_op_when_multiple(self):
        """`docs,code,prompt` → primeira (`docs`) é usada no passo 1."""
        req = WizardSkillRequest(description="x", mcp_tool_ids=["tool-id-1"])
        system, _ = _build_wizard_prompt(
            req, self._mcp_only_bindings(ops="docs,code,prompt"), "fast",
        )
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        # Procura a linha do passo 1
        assert "operation=docs" in obligatory
        # NÃO a segunda nem a terceira
        m_step1 = re.search(r"1\. \*\*Chame\*\*[^\n]*", obligatory)
        assert m_step1
        assert "operation=code" not in m_step1.group(0)
        assert "operation=prompt" not in m_step1.group(0)


class TestRegressionContext7BugV2:
    """Regressão do bug v2 (2026-05-29 #2): mesmo após PR #180 corrigir o
    Workflow passivo, SKILL gerada pediu `operation=search` em Context7
    (que só aceita docs/code/prompt). Servidor MCP devolveu erro, LLM em
    runtime respondeu "não consegui acessar".

    Causa: `## Tool Bindings` no obligatory_sections NÃO listava as
    operations declaradas no Registry — só id+name+description. LLM
    gerador via exemplo `operation=docs` no _mcp_block mas, sem lista
    oficial das operations no bloco obrigatório, escolheu "search" por
    sonoridade ("search by pattern_type").

    Fix: (a) incluir operations EXPLICITAMENTE em cada linha do bloco
    `## Tool Bindings`, (b) regra crítica anti-invent no _mcp_block.
    """

    def _ctx7_bindings(self):
        return {
            "mcp_tools": [{
                "id": "481c5fa3-36bc-4d05-97ff-d502d93521ff",
                "name": "Context 7 MCP Server",
                "description": "Plataforma Context7 para documentação atualizada de qualquer prompt",
                "operations": "docs,code,prompt",
            }],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }

    def test_tool_bindings_block_lists_operations_explicitly(self):
        """Cada tool no obligatory `## Tool Bindings` precisa listar suas
        operations declaradas — sem isso, LLM gerador inventa nomes."""
        req = WizardSkillRequest(
            description="x",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # Localiza o bloco obrigatório
        start = system.find("=== SEÇÕES OBRIGATÓRIAS")
        end = system.find("=== FIM DAS SEÇÕES OBRIGATÓRIAS")
        obligatory = system[start:end]
        # As 3 operations canônicas do Context7 precisam estar no bloco
        assert "docs" in obligatory
        assert "code" in obligatory
        assert "prompt" in obligatory
        # E com marcador "Operations" explícito (não só citadas no nome)
        assert "Operations" in obligatory

    def test_tool_bindings_uses_imperative_phrase_about_operations(self):
        """A linha das operations precisa ser imperativa pra o LLM gerador
        entender que NÃO pode inventar."""
        req = WizardSkillRequest(
            description="x",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # Frase com "APENAS" ou "Use APENAS" perto das operations
        assert "use APENAS" in system or "Use APENAS" in system

    def test_mcp_block_has_critical_rule_about_inventing_operations(self):
        """_mcp_block precisa ter regra crítica sobre não inventar operations
        (search/query/fetch/get) que não estejam declaradas."""
        req = WizardSkillRequest(
            description="x",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # Regra crítica explícita
        assert "REGRA CRÍTICA" in system and "operations" in system
        # Cita os nomes inventados comuns como proibidos
        # (o LLM gerador no caso real escolheu 'search')
        assert "search" in system  # citado como exemplo do que NÃO usar
        # NUNCA invente
        assert "NUNCA invente" in system or "nunca invente" in system.lower()

    def test_mcp_block_lists_actual_operations_in_context(self):
        """_mcp_block deve listar as operations REAIS da primeira tool
        (não só citar o exemplo first_op). Sem isso, o LLM gerador só vê
        1 operation no exemplo e não sabe das outras."""
        req = WizardSkillRequest(
            description="x",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        # Procura padrão "Operations disponíveis em `Context 7 MCP Server`:
        # `docs,code,prompt`"
        assert "Operations disponíveis em" in system
        # As 3 operations todas presentes no _mcp_block (não só na primeira)
        # Vai procurar na região do _mcp_block (após "[MCP]")
        idx_mcp = system.find("[MCP]")
        idx_rag = system.find("[RAG]")
        end_mcp = idx_rag if idx_rag > idx_mcp else len(system)
        mcp_section = system[idx_mcp:end_mcp]
        assert "docs" in mcp_section
        assert "code" in mcp_section
        assert "prompt" in mcp_section

    def test_warning_mentions_runtime_consequence(self):
        """Pra o LLM gerador internalizar a regra, é útil explicar a
        consequência: servidor MCP recusa + usuário vê erro. Isso conecta
        a regra com a experiência ruim real reportada."""
        req = WizardSkillRequest(
            description="x",
            mcp_tool_ids=["481c5fa3-36bc-4d05-97ff-d502d93521ff"],
        )
        system, _ = _build_wizard_prompt(req, self._ctx7_bindings(), "fast")
        low = system.lower()
        # Menção à consequência: servidor recusa
        assert "rejeita" in low or "recusa" in low or "rejeitar" in low

    def test_tool_without_declared_operations_falls_back_gracefully(self):
        """Tool MCP cadastrada sem operations no Registry — bloco precisa
        sobreviver (sem KeyError, sem texto vazio) e usar fallback."""
        bindings = {
            "mcp_tools": [{
                "id": "no-ops",
                "name": "Mystery Tool",
                "description": "Tool registrada sem operations declaradas",
                "operations": "",  # vazio explícito
            }],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        req = WizardSkillRequest(description="x", mcp_tool_ids=["no-ops"])
        system, _ = _build_wizard_prompt(req, bindings, "fast")
        # Não deve estourar; _mcp_block deve indicar fallback
        assert "Mystery Tool" in system
        assert "[MCP]" in system
        # Quando operations vazias, fallback display
        assert "não declaradas" in system.lower() or "operations" in system.lower()


# ═════════════════════════════════════════════════════════════════
# Regressão de combos — garante que adição do mcp_invocation_rules
# não quebra paths existentes de RAG/API/Tables/Output Shape/kind=router
# ═════════════════════════════════════════════════════════════════
#
# Pergunta do user (2026-05-29): "considerou que esse Wizard precisa garantir
# que tudo que estava funcionando se mantenha funcionando? chamada de outros
# MCPs, chamada de API, uso de RAG entre outras?"
#
# Cobertura específica de combos. Cada teste roda _build_wizard_prompt com
# uma combinação e valida:
# - Cada path emite SUA seção obrigatória
# - Nenhuma seção é duplicada
# - Adição do mcp_invocation_rules não suprime nenhum outro path
# - Ordem das seções permanece coerente


# ═════════════════════════════════════════════════════════════════
# Regras gerais comuns a QUALQUER binding (G1-G4) — pergunta do user
# 2026-05-29: "plataforma é Skill-based, precisamos ser assertivos e
# precisos — Skills chamam API ou RAG ou MCP ou Tabelas, é geral"
# ═════════════════════════════════════════════════════════════════


class TestCommonBindingRulesAreGeneral:
    """Regras G1-G4 (verbo imperativo, frases proibidas, rastreabilidade,
    proibição de 'nenhuma fonte externa') aparecem pra QUALQUER binding —
    não só MCP. Garantia de que generalização foi pra frente.
    """

    def _make_bindings(self, *, mcp=False, rag=False, api=False, tables=False):
        return {
            "mcp_tools": [{"id": "t1", "name": "Tool X", "description": "D", "operations": "search"}] if mcp else [],
            "rag_sources": [{"id": "s1", "name": "Bases", "confidentiality_label": "internal"}] if rag else [],
            "api_endpoints": [{"ep_id": "e1", "conn_id": "c1", "ep_name": "EP", "method": "GET", "url": "https://x/y"}] if api else [],
            "data_tables": [{"urn": "urn:t:x", "name": "Tab", "row_count": 10, "schema_summary": "a,b"}] if tables else [],
        }

    @pytest.mark.parametrize("kind,kwargs,req_kwargs", [
        ("mcp", {"mcp": True},     {"mcp_tool_ids": ["t1"]}),
        ("rag", {"rag": True},     {"source_ids": ["s1"]}),
        ("api", {"api": True},     {"api_keys": ["c1:e1"]}),
        ("tab", {"tables": True},  {"table_ids": ["urn:t:x"]}),
    ])
    def test_g1_imperative_verb_required_for_any_binding(self, kind, kwargs, req_kwargs):
        """G1: verbo imperativo é exigido pra qualquer binding."""
        req = WizardSkillRequest(description="x", **req_kwargs)
        system, _ = _build_wizard_prompt(req, self._make_bindings(**kwargs), "standard")
        assert "VERBO IMPERATIVO" in system
        # Lista de verbos aceitos visível (entre os imperativos canônicos)
        for verb in ("Chame", "Consulte", "Execute"):
            assert verb in system, f"verbo {verb!r} ausente pra binding={kind}"

    @pytest.mark.parametrize("kind,kwargs,req_kwargs", [
        ("mcp", {"mcp": True},     {"mcp_tool_ids": ["t1"]}),
        ("rag", {"rag": True},     {"source_ids": ["s1"]}),
        ("api", {"api": True},     {"api_keys": ["c1:e1"]}),
        ("tab", {"tables": True},  {"table_ids": ["urn:t:x"]}),
    ])
    def test_g1_passive_verbs_listed_as_blocked(self, kind, kwargs, req_kwargs):
        """G1: verbos passivos do bug Context7 marcados como proibidos
        pra qualquer tipo de binding."""
        req = WizardSkillRequest(description="x", **req_kwargs)
        system, _ = _build_wizard_prompt(req, self._make_bindings(**kwargs), "standard")
        # Subset dos verbos passivos críticos (não exaustivo, mas o do bug)
        assert "enriquecimento" in system
        assert "usando o binding" in system
        assert "INSUFICIENTES" in system or "PROIBIDOS" in system

    @pytest.mark.parametrize("kind,kwargs,req_kwargs", [
        ("mcp", {"mcp": True},     {"mcp_tool_ids": ["t1"]}),
        ("rag", {"rag": True},     {"source_ids": ["s1"]}),
        ("api", {"api": True},     {"api_keys": ["c1:e1"]}),
        ("tab", {"tables": True},  {"table_ids": ["urn:t:x"]}),
    ])
    def test_g2_internal_phrases_blocked_for_any_binding(self, kind, kwargs, req_kwargs):
        """G2: frases tipo "template interno" / "conhecimento próprio"
        proibidas pra qualquer binding."""
        req = WizardSkillRequest(description="x", **req_kwargs)
        system, _ = _build_wizard_prompt(req, self._make_bindings(**kwargs), "standard")
        assert "template interno" in system
        assert "conhecimento próprio" in system
        assert "FRASES PROIBIDAS" in system

    @pytest.mark.parametrize("kind,kwargs,req_kwargs", [
        ("mcp", {"mcp": True},     {"mcp_tool_ids": ["t1"]}),
        ("rag", {"rag": True},     {"source_ids": ["s1"]}),
        ("api", {"api": True},     {"api_keys": ["c1:e1"]}),
        ("tab", {"tables": True},  {"table_ids": ["urn:t:x"]}),
    ])
    def test_g3_traceability_pattern_visible_for_any_binding(self, kind, kwargs, req_kwargs):
        """G3: padrão Entrada → Ação → Resposta → Saída final exigido em
        Examples pra qualquer binding."""
        req = WizardSkillRequest(description="x", **req_kwargs)
        system, _ = _build_wizard_prompt(req, self._make_bindings(**kwargs), "standard")
        assert "Entrada:" in system
        assert "Saída final:" in system
        assert "alucinar" in system.lower()

    @pytest.mark.parametrize("kind,kwargs,req_kwargs", [
        ("mcp", {"mcp": True},     {"mcp_tool_ids": ["t1"]}),
        ("rag", {"rag": True},     {"source_ids": ["s1"]}),
        ("api", {"api": True},     {"api_keys": ["c1:e1"]}),
        ("tab", {"tables": True},  {"table_ids": ["urn:t:x"]}),
    ])
    def test_g4_no_external_source_phrase_blocked_for_any_binding(self, kind, kwargs, req_kwargs):
        """G4: 'nenhuma fonte externa autorizada' proibida quando há
        QUALQUER binding (não só MCP)."""
        req = WizardSkillRequest(description="x", **req_kwargs)
        system, _ = _build_wizard_prompt(req, self._make_bindings(**kwargs), "standard")
        assert "nenhuma fonte externa autorizada" in system.lower()
        assert "NUNCA escreva" in system


# ═════════════════════════════════════════════════════════════════
# Sub-blocos específicos por tipo de binding ([MCP], [RAG], [API], [TABLES])
# ═════════════════════════════════════════════════════════════════


class TestRAGSubBlock:
    """Sub-bloco [RAG]: documenta consulta RAG mesmo sabendo que engine
    faz retrieval automático em RetrieveEvidence."""

    def _rag_bindings(self):
        return {
            "mcp_tools": [],
            "rag_sources": [
                {"id": "s1", "name": "Manuais Internos", "confidentiality_label": "internal"},
            ],
            "data_tables": [], "api_endpoints": [],
        }

    def test_rag_block_present_when_rag_declared(self):
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        system, _ = _build_wizard_prompt(req, self._rag_bindings(), "standard")
        assert "[RAG]" in system

    def test_rag_block_cites_source_name(self):
        """Nome exato da base deve aparecer no sub-bloco pra LLM gerador
        construir Workflow nominalmente correto."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        system, _ = _build_wizard_prompt(req, self._rag_bindings(), "standard")
        assert "Manuais Internos" in system

    def test_rag_block_uses_consultative_verbs(self):
        """RAG usa verbo 'Consulte'/'Recupere'/'Busque em' — não 'Chame'
        (chamar é vocabulário de MCP/API)."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        system, _ = _build_wizard_prompt(req, self._rag_bindings(), "standard")
        # Pelo menos um dos verbos canônicos de RAG
        assert "Consulte" in system or "Recupere" in system or "Busque em" in system

    def test_rag_block_mentions_engine_automatic_retrieval(self):
        """LLM gerador precisa entender que engine faz retrieval automático
        — Workflow documenta pra coerência semântica, não pra acionar."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        system, _ = _build_wizard_prompt(req, self._rag_bindings(), "standard")
        assert "RetrieveEvidence" in system or "retrieval automatic" in system.lower() or "automaticamente" in system

    def test_rag_block_forbids_hallucinated_facts(self):
        """Sub-bloco RAG instrui LLM gerador a documentar que resposta
        DEVE referenciar chunks recuperados — proteção contra alucinação."""
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        system, _ = _build_wizard_prompt(req, self._rag_bindings(), "standard")
        assert "chunks recuperados" in system or "referenciar" in system

    def test_rag_block_absent_when_no_rag(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "X", "description": "Y", "operations": "z"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "[RAG]" not in system


class TestAPISubBlock:
    """Sub-bloco [API]: documenta execução de endpoint declarativo. Engine
    executa sem LLM no caminho, mas SKILL.md precisa ser explícito pro
    LLM saber referenciar a resposta da API."""

    def _api_bindings(self):
        return {
            "mcp_tools": [], "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [{
                "ep_id": "ep-cep", "conn_id": "conn-correios",
                "ep_name": "Consulta CEP", "method": "GET",
                "url": "https://viacep.com.br/ws/{cep}/json",
            }],
        }

    def test_api_block_present_when_endpoints_declared(self):
        req = WizardSkillRequest(description="x", api_keys=["conn-correios:ep-cep"])
        system, _ = _build_wizard_prompt(req, self._api_bindings(), "standard")
        assert "[API]" in system

    def test_api_block_cites_endpoint_name_and_method(self):
        req = WizardSkillRequest(description="x", api_keys=["conn-correios:ep-cep"])
        system, _ = _build_wizard_prompt(req, self._api_bindings(), "standard")
        assert "Consulta CEP" in system
        assert "GET" in system

    def test_api_block_uses_execute_verbs(self):
        """API usa 'Execute'/'Acione' — não 'Consulte' (vocabulário RAG)."""
        req = WizardSkillRequest(description="x", api_keys=["conn-correios:ep-cep"])
        system, _ = _build_wizard_prompt(req, self._api_bindings(), "standard")
        assert "Execute" in system or "Acione" in system

    def test_api_block_mentions_declarative_mode(self):
        """LLM gerador precisa saber que execution_mode=declarative é
        obrigatório no frontmatter pra essa skill."""
        req = WizardSkillRequest(description="x", api_keys=["conn-correios:ep-cep"])
        system, _ = _build_wizard_prompt(req, self._api_bindings(), "standard")
        assert "execution_mode: declarative" in system or "declarativo" in system.lower()

    def test_api_block_forbids_hallucinated_response_fields(self):
        """Bug clássico: LLM inventa campos no Output Contract que a API
        não retorna. Sub-bloco API alerta contra isso."""
        req = WizardSkillRequest(description="x", api_keys=["conn-correios:ep-cep"])
        system, _ = _build_wizard_prompt(req, self._api_bindings(), "standard")
        assert "não inventar campos" in system or "Output Contract DEVE refletir" in system

    def test_api_block_absent_when_no_api(self):
        req = WizardSkillRequest(description="x", source_ids=["s1"])
        bindings = {
            "mcp_tools": [], "rag_sources": [{"id": "s1", "name": "X", "confidentiality_label": "internal"}],
            "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "[API]" not in system


class TestTablesSubBlock:
    """Sub-bloco [TABLES]: query PARAMETRIZADA no bloco ## Data Tables (engine
    executa via DuckDB com bind vars; o LLM NÃO escreve SQL). Exige
    execution_mode: declarative — sem ele a tabela nunca é lida (correção 2026-06-09:
    o modelo antigo 'LLM gera SQL' gerava skill que não consultava a tabela)."""

    def _tables_bindings(self):
        return {
            "mcp_tools": [], "rag_sources": [], "api_endpoints": [],
            "data_tables": [{
                "urn": "urn:table:vendas-2026", "name": "Vendas 2026",
                "row_count": 50000, "schema_summary": "data:DATE, cliente:VARCHAR, valor:DOUBLE",
                "columns": ["data", "cliente", "valor", "regiao"], "suggested_pk": "cliente",
            }],
        }

    def test_tables_block_present_when_tables_declared(self):
        req = WizardSkillRequest(description="x", table_ids=["urn:table:vendas-2026"])
        system, _ = _build_wizard_prompt(req, self._tables_bindings(), "standard")
        assert "[TABLES]" in system

    def test_tables_block_cites_urn(self):
        req = WizardSkillRequest(description="x", table_ids=["urn:table:vendas-2026"])
        system, _ = _build_wizard_prompt(req, self._tables_bindings(), "standard")
        assert "urn:table:vendas-2026" in system

    def test_tables_block_describes_parametrized_query(self):
        """Tabela = query PARAMETRIZADA (não LLM escrevendo SQL)."""
        req = WizardSkillRequest(description="x", table_ids=["urn:table:vendas-2026"])
        system, _ = _build_wizard_prompt(req, self._tables_bindings(), "standard")
        assert "parametrizada" in system.lower()

    def test_tables_block_states_llm_does_not_write_sql(self):
        """O LLM NÃO gera SQL — a query é parametrizada e o engine executa via DuckDB."""
        req = WizardSkillRequest(description="x", table_ids=["urn:table:vendas-2026"])
        system, full_user = _build_wizard_prompt(req, self._tables_bindings(), "standard")
        combined = system + "\n" + full_user
        assert "DuckDB" in system
        assert "não escreve sql" in combined.lower()
        assert "LLM gera SQL" not in combined  # modelo errado banido
        # frontmatter declarativo é exigido (senão a tabela nunca é lida)
        assert "execution_mode: declarative" in combined

    def test_tables_block_forbids_invented_columns(self):
        """LLM tende a inventar colunas — o sub-bloco proíbe colunas fora do schema."""
        req = WizardSkillRequest(description="x", table_ids=["urn:table:vendas-2026"])
        system, _ = _build_wizard_prompt(req, self._tables_bindings(), "standard")
        assert "não invente colunas" in system.lower() or "schema declarado" in system

    def test_tables_block_absent_when_no_tables(self):
        req = WizardSkillRequest(description="x", mcp_tool_ids=["t1"])
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "X", "description": "Y", "operations": "z"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "[TABLES]" not in system


class TestComboRegressions:
    """Combos de bindings — proteção contra interação cruzada do
    binding_invocation_rules com os outros paths.
    """

    def test_mcp_plus_api_emits_both_sections(self):
        """Skill com MCP + API endpoints — ambas seções obrigatórias presentes,
        execution_mode declarative ainda exigido pra API."""
        req = WizardSkillRequest(
            description="x", mcp_tool_ids=["t1"], api_keys=["conn-1:ep-1"],
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "MCP A", "description": "D", "operations": "search"}],
            "rag_sources": [],
            "data_tables": [],
            "api_endpoints": [{
                "ep_id": "ep-1", "conn_id": "conn-1", "ep_name": "Endpoint X",
                "method": "GET", "url": "https://api.example.com/x",
            }],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # MCP section
        assert "## Tool Bindings" in system
        assert "`MCP A`" in system or "MCP A" in system
        # API section
        assert "## API Bindings" in system
        assert "ep-1" in system
        assert "conn-1" in system
        # Declarative mode obrigatório (API path)
        assert "execution_mode: declarative" in system
        # MCP rules emitidas
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system

    def test_mcp_plus_data_tables_emits_both_sections(self):
        """MCP + Data Tables — ambas presentes, sem conflito."""
        req = WizardSkillRequest(
            description="x", mcp_tool_ids=["t1"], table_ids=["tbl-1"],
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "MCP A", "description": "D", "operations": "search"}],
            "rag_sources": [],
            "data_tables": [{
                "urn": "urn:table:vendas", "name": "Vendas",
                "row_count": 100, "schema_summary": "id, valor",
            }],
            "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # MCP section
        assert "## Tool Bindings" in system
        # Tables section
        assert "## Data Tables" in system
        assert "urn:table:vendas" in system
        # MCP rules emitidas
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system

    def test_mcp_plus_output_shape_preset(self):
        """MCP + length_preset — Output Shape emitido em adição ao MCP rules."""
        req = WizardSkillRequest(
            description="x", mcp_tool_ids=["t1"], length_preset="analysis",
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "MCP A", "description": "D", "operations": "search"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # MCP section + rules
        assert "## Tool Bindings" in system
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system
        # Output Shape preset emitido
        assert "## Output Shape" in system
        assert "length_preset: analysis" in system

    def test_router_kind_with_mcp_still_works(self):
        """kind=router + MCP — header reflete router, MCP rules ativas, sem
        regressão. Roteadores costumam declarar MCPs delegáveis aos subagentes."""
        req = WizardSkillRequest(
            description="x", kind="router", domain="financeiro",
            mcp_tool_ids=["t1"],
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "MCP A", "description": "D", "operations": "route"}],
            "rag_sources": [], "data_tables": [], "api_endpoints": [],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        # Kind correto no URN exemplo
        assert "urn:skill:financeiro:router:" in system
        assert "kind: router" in system
        # MCP rules ativas
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system

    @staticmethod
    def _extract_obligatory_block(system_prompt: str) -> str:
        """Extrai o conteúdo entre os marcadores SEÇÕES OBRIGATÓRIAS.

        O system_prompt tem 3 blocos que podem mencionar nomes de seções:
        (1) anti_halluc_rules — cita ## Evidence Policy em proibições
        (2) mcp_invocation_rules — cita ## Examples no formato de tool call
        (3) template canônico — descreve cada seção do SKILL.md
        (4) obligatory_block — entre marcadores ===, com YAMLs reais

        Pra checar "seção foi REALMENTE emitida no bloco obrigatório",
        precisa extrair (4) — só lá a ordem e a unicidade importam.
        """
        start_marker = "=== SEÇÕES OBRIGATÓRIAS A INCLUIR NO SKILL.md ==="
        end_marker = "=== FIM DAS SEÇÕES OBRIGATÓRIAS ==="
        i = system_prompt.find(start_marker)
        j = system_prompt.find(end_marker)
        assert i >= 0 and j > i, (
            "marcadores de obligatory_block ausentes — refactor mudou estrutura?"
        )
        return system_prompt[i:j]

    def test_all_bindings_combo_no_duplication(self):
        """O combo MCP + RAG + API + Tables — todos os 4 paths emitem,
        nenhum suprime o outro, mcp_invocation_rules não duplica nenhum.

        Esta é a regressão mais ariscada: combinatorialmente, nenhum teste
        anterior cobre isso. Mudança do mcp_invocation_rules poderia em
        teoria interferir nas outras seções.
        """
        req = WizardSkillRequest(
            description="skill que usa tudo",
            mcp_tool_ids=["t1"],
            source_ids=["s1"],
            table_ids=["tbl-1"],
            api_keys=["conn-1:ep-1"],
            min_relevance=0.15,
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "Tool MCP", "description": "Desc MCP", "operations": "search"}],
            "rag_sources": [{"id": "s1", "name": "Bases", "confidentiality_label": "internal"}],
            "data_tables": [{
                "urn": "urn:table:x", "name": "X",
                "row_count": 50, "schema_summary": "col1",
            }],
            "api_endpoints": [{
                "ep_id": "ep-1", "conn_id": "conn-1", "ep_name": "EP",
                "method": "POST", "url": "https://api.x/y",
            }],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        obligatory = self._extract_obligatory_block(system)

        # Todas as 4 seções obrigatórias presentes NO BLOCO obrigatório
        sections = ["## Tool Bindings", "## Evidence Policy", "## Data Tables", "## API Bindings"]
        for sec in sections:
            assert sec in obligatory, f"seção {sec!r} ausente no obligatory_block"

        # Nenhuma seção duplica DENTRO do bloco obrigatório (cada path emite 1x)
        for sec in sections:
            count = obligatory.count(sec)
            assert count == 1, (
                f"seção {sec!r} duplicou ({count}x) dentro do obligatory_block — "
                "concat path bugado, algum path emitiu 2x"
            )

        # MCP rules ativas (no system_prompt, fora do obligatory_block)
        assert "REGRAS DE INVOCAÇÃO DE BINDINGS" in system

        # IDs específicos preservados em cada seção (no obligatory_block)
        assert "s1" in obligatory           # RAG
        assert "ep-1" in obligatory         # API
        assert "urn:table:x" in obligatory  # Tables
        assert "t1" in obligatory           # MCP (UUID/id)

        # Threshold do RAG preservado (mudança não quebrou path de min_relevance)
        assert "min_relevance: 0.15" in obligatory

        # Execution mode declarative (do API path) preservado
        assert "execution_mode: declarative" in obligatory

    def test_combo_section_ordering_stable_in_obligatory_block(self):
        """Ordem das seções DENTRO do obligatory_block precisa permanecer
        estável: Tool Bindings → Evidence Policy → Data Tables →
        API Bindings → Execution Profile → Output Shape.

        Mudança do mcp_invocation_rules NÃO deve alterar essa ordem (caso
        contrário SKILLs geradas antes vs depois ficariam visualmente
        diferentes em diff, dificultando code review).
        """
        req = WizardSkillRequest(
            description="x", mcp_tool_ids=["t1"], source_ids=["s1"],
            table_ids=["tbl-1"], api_keys=["conn-1:ep-1"],
            length_preset="digest",
        )
        bindings = {
            "mcp_tools": [{"id": "t1", "name": "MCP A", "description": "D", "operations": "search"}],
            "rag_sources": [{"id": "s1", "name": "Bases", "confidentiality_label": "internal"}],
            "data_tables": [{"urn": "urn:t:x", "name": "X", "row_count": 1, "schema_summary": "a"}],
            "api_endpoints": [{"ep_id": "ep-1", "conn_id": "conn-1", "ep_name": "EP", "method": "GET", "url": "https://x/y"}],
        }
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        obligatory = self._extract_obligatory_block(system)

        # Ordem esperada DENTRO do obligatory_block (ordem das chamadas append)
        order = ["## Tool Bindings", "## Evidence Policy", "## Data Tables",
                 "## API Bindings", "## Execution Profile", "## Output Shape"]
        positions = [obligatory.find(s) for s in order]
        # Nenhuma seção ausente
        assert all(p > 0 for p in positions), (
            f"alguma seção ausente do obligatory_block: {dict(zip(order, positions))}"
        )
        # Ordem monotônica crescente
        assert positions == sorted(positions), (
            f"ordem das seções no obligatory_block mudou — "
            f"esperado {order}, posições {positions}"
        )


# ═════ Contrato de Decisão no wizard (Cond-C.2, 36.2.0) ═════


class TestWizardDecisions:
    """`decisions` no request vira a seção ## Decisions SELADA no prompt do
    wizard. Validação ACIONÁVEL no formulário — o parser de runtime descartaria
    campo inválido em silêncio e o contrato nasceria morto."""

    def test_emite_secao_decisions_roundtrip_com_o_parser(self):
        req = WizardSkillRequest(
            description="triagem",
            decisions={"escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"]},
        )
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "## Decisions" in system
        # roundtrip REAL: a seção emitida parseia para o MESMO contrato
        from app.skill_parser.decisions_schema import extract_decisions_schema
        sec = system[system.find("## Decisions"):]
        assert extract_decisions_schema(sec) == {
            "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"]}

    def test_sem_decisions_nao_emite_secao(self):
        req = WizardSkillRequest(description="x")
        bindings = {"mcp_tools": [], "rag_sources": [], "data_tables": [], "api_endpoints": []}
        system, _ = _build_wizard_prompt(req, bindings, "standard")
        assert "## Decisions" not in system

    def test_validador_rejeita_com_mensagem_acionavel(self):
        import pytest as _pt
        # campo acentuado
        with _pt.raises(Exception) as e1:
            WizardSkillRequest(description="x", decisions={"situação": ["a"]})
        assert "identificador ASCII" in str(e1.value)
        # nome reservado de dict (decision.items = método no Jinja)
        with _pt.raises(Exception) as e2:
            WizardSkillRequest(description="x", decisions={"items": ["a"]})
        assert "reservado" in str(e2.value)
        # separador da linha DECISAO no valor
        with _pt.raises(Exception) as e3:
            WizardSkillRequest(description="x", decisions={"parecer": ["sim, com ressalvas"]})
        assert "separadores da linha DECISAO" in str(e3.value)
        # campo sem valores
        with _pt.raises(Exception) as e4:
            WizardSkillRequest(description="x", decisions={"escalar": []})
        assert "ao menos 1 valor" in str(e4.value)

    def test_validador_normaliza_e_deduplica(self):
        req = WizardSkillRequest(
            description="x", decisions={" escalar ": ["  sim ", "sim", "não", ""]})
        assert req.decisions == {"escalar": ["sim", "não"]}

    def test_decisions_vazio_vira_none(self):
        assert WizardSkillRequest(description="x", decisions={}).decisions is None

    def test_validador_rejeita_borda_stripada_e_deduplica_por_norm(self):
        import pytest as _pt
        # borda que o runtime stripa ('aprovado.' nasceria morto — review)
        with _pt.raises(Exception) as e1:
            WizardSkillRequest(description="x", decisions={"parecer": ["aprovado."]})
        assert "BORDAS" in str(e1.value)
        # dedup pela MESMA norma do runtime (caixa/acento)
        req = WizardSkillRequest(description="x", decisions={"sev": ["Alta", "alta", "álta"]})
        assert req.decisions == {"sev": ["Alta"]}
        # campos que colidem após strip
        with _pt.raises(Exception) as e2:
            WizardSkillRequest(description="x", decisions={"escalar": ["sim"], " escalar ": ["não"]})
        assert "duplicado" in str(e2.value)

    def test_pos_geracao_forca_contrato_selado(self):
        from app.routes.wizard import _ensure_decisions_contract
        from app.skill_parser.decisions_schema import extract_decisions_schema
        contrato = {"escalar": ["sim", "não"]}
        # drift do LLM (traduziu) → seção substituída pela canônica
        drift = "# S\n## Purpose\np\n\n## Decisions\n```json\n{\"escalate\": [\"yes\"]}\n```\n\n## Guardrails\ng\n"
        fixed = _ensure_decisions_contract(drift, contrato)
        assert extract_decisions_schema(fixed) == contrato
        assert "escalate" not in fixed
        # omissão → seção inserida (antes de ## Guardrails)
        omitiu = "# S\n## Purpose\np\n\n## Guardrails\ng\n"
        fixed2 = _ensure_decisions_contract(omitiu, contrato)
        assert extract_decisions_schema(fixed2) == contrato
        assert fixed2.find("## Decisions") < fixed2.find("## Guardrails")
        # já correto → intocado (idempotente)
        assert _ensure_decisions_contract(fixed, contrato) == fixed
        # sem contrato declarado → no-op
        assert _ensure_decisions_contract(drift, None) == drift
