"""Testes E2E: parser ## Data Tables + execução no declarative_engine.

Estratégia:
- Parser: usa parse_skill_md diretamente (sem mocks).
- Engine: mocka tabular_execute_query + find_by_urn_with_ks +
  interactions_repo + binding_executions_repo. NÃO toca em Postgres
  nem em DuckDB real — a confiança no service tabular vem de
  tests/test_data_tables.py.

Cobre:
- Parser aceita formatos YAML aceitos, descarta itens inválidos
- Engine roda data_tables ANTES dos bindings, expõe resultado no context
- output_mapping (dict syntax + list syntax + ausente = default)
- on_error fail vs continue
- dry_run pula a fase tabular
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agents import declarative_engine as de
from app.skill_parser.parser import (
    ParsedSkill,
    SkillFrontmatter,
    parse_skill_md,
    _parse_data_tables,
)


# ═══════════════════════════════════════════════════════════════
# 1. Parser — _parse_data_tables direto
# ═══════════════════════════════════════════════════════════════


class TestParseDataTables:
    def test_fence_with_tables_key(self):
        text = """```yaml
tables:
  - id: vendas
    table_ref: urn:table:abc:vendas:1
    query:
      select: [valor]
      limit: 10
```"""
        out = _parse_data_tables(text)
        assert len(out) == 1
        assert out[0]["id"] == "vendas"
        assert out[0]["table_ref"] == "urn:table:abc:vendas:1"

    def test_fence_with_direct_list(self):
        text = """```yaml
- id: vendas
  table_ref: urn:table:abc:vendas:1
  query:
    select: [valor]
```"""
        out = _parse_data_tables(text)
        assert len(out) == 1
        assert out[0]["id"] == "vendas"

    def test_inline_without_fence(self):
        text = """tables:
  - id: vendas
    table_ref: urn:table:abc:vendas:1
"""
        out = _parse_data_tables(text)
        assert len(out) == 1

    def test_empty_returns_empty_list(self):
        assert _parse_data_tables("") == []
        assert _parse_data_tables("   ") == []

    def test_malformed_yaml_returns_empty(self):
        text = """```yaml
tables:
  - id: vendas
    table_ref: x
  invalid: [unclosed
```"""
        assert _parse_data_tables(text) == []

    def test_items_missing_id_discarded(self):
        text = """```yaml
tables:
  - id: ok
    table_ref: urn:table:x:y:1
  - table_ref: urn:table:x:z:1
  - id: only_id
```"""
        out = _parse_data_tables(text)
        assert len(out) == 1
        assert out[0]["id"] == "ok"

    def test_non_dict_items_discarded(self):
        text = """```yaml
tables:
  - "string item"
  - 42
  - id: ok
    table_ref: urn:table:x:y:1
```"""
        out = _parse_data_tables(text)
        assert len(out) == 1


# ═══════════════════════════════════════════════════════════════
# 2. Parser — parse_skill_md full pipeline
# ═══════════════════════════════════════════════════════════════


def _build_skill_md(data_tables_yaml: str, with_bindings: bool = False) -> str:
    bindings = """
## API Bindings
```yaml
- id: b1
  connector: TestAPI
  request:
    method: GET
    path: /x
```
""" if with_bindings else "## API Bindings\n\n"

    return f"""---
id: urn:skill:dom:subagent:teste
version: 0.1.0
kind: subagent
owner: equipe
stability: alpha
execution_mode: declarative
---

# Skill Teste

## Purpose
x

## Activation Criteria
x

## Inputs
x

## Workflow
x

## Tool Bindings
x

## Output Contract
x

## Failure Modes
x
{bindings}
## Data Tables
{data_tables_yaml}
"""


class TestParseSkillMdWithDataTables:
    def test_skill_with_only_data_tables_is_valid(self):
        yaml_block = """```yaml
tables:
  - id: vendas
    table_ref: urn:table:abc:vendas:1
    query:
      select: [valor]
      limit: 10
```"""
        skill = parse_skill_md(_build_skill_md(yaml_block, with_bindings=False))
        # Tem data_tables_parsed
        assert len(skill.data_tables_parsed) == 1
        # NÃO deve reclamar de "execution_mode=declarative exige API Bindings"
        # — desde que tenha data_tables
        decl_errors = [e for e in skill.validation_errors if "declarative" in e.lower()]
        assert decl_errors == []

    def test_skill_without_bindings_nor_data_tables_complains(self):
        skill = parse_skill_md(_build_skill_md("\n", with_bindings=False))
        # Sem bindings nem tables → deve haver erro de declarative
        decl_errors = [e for e in skill.validation_errors if "declarative" in e.lower()]
        assert len(decl_errors) >= 1

    def test_skill_with_both_bindings_and_tables(self):
        yaml_block = """```yaml
tables:
  - id: vendas
    table_ref: urn:table:abc:vendas:1
```"""
        skill = parse_skill_md(_build_skill_md(yaml_block, with_bindings=True))
        assert len(skill.api_bindings_parsed) == 1
        assert len(skill.data_tables_parsed) == 1

    def test_skill_to_db_dict_includes_data_tables(self):
        from app.skill_parser.parser import skill_to_db_dict
        yaml_block = """```yaml
tables:
  - id: t1
    table_ref: urn:table:x:y:1
```"""
        skill = parse_skill_md(_build_skill_md(yaml_block, with_bindings=False))
        d = skill_to_db_dict(skill)
        assert "data_tables" in d
        assert "tables:" in d["data_tables"]


# ═══════════════════════════════════════════════════════════════
# 3. Engine — _execute_data_tables_phase + execute_declarative
# ═══════════════════════════════════════════════════════════════


class FakeRepo:
    def __init__(self, store: dict):
        self.store = store

    async def find_by_id(self, id_):
        return dict(self.store[id_]) if id_ in self.store else None

    async def find_all(self, limit=100, **filters):
        rows = list(self.store.values())
        for k, v in filters.items():
            rows = [r for r in rows if r.get(k) == v]
        return rows[:limit]

    async def create(self, data):
        self.store[data["id"]] = dict(data)
        return data

    async def update(self, id_, data):
        if id_ not in self.store:
            return None
        self.store[id_].update(data)
        return dict(self.store[id_])

    async def delete(self, id_):
        return self.store.pop(id_, None) is not None


@pytest.fixture
def fake_repos(monkeypatch):
    """Substitui repos do engine por in-memory."""
    connectors: dict = {}
    api_logs: dict = {}
    interactions: dict = {}
    bindings: dict = {}

    monkeypatch.setattr(de, "api_connectors_repo", FakeRepo(connectors))
    monkeypatch.setattr(de, "api_call_logs_repo", FakeRepo(api_logs))
    monkeypatch.setattr(de, "interactions_repo", FakeRepo(interactions))
    monkeypatch.setattr(de, "binding_executions_repo", FakeRepo(bindings))

    return {
        "binding_executions": bindings,
        "interactions": interactions,
    }


@pytest.fixture
def fake_tabular(monkeypatch):
    """Mocka resolução de tabela + execute_query do service tabular.

    Permite injetar resultados controlados sem precisar de DuckDB nem
    Postgres real. O fixture retorna handles para configurar:
    - tables_by_ref: {urn: row_dict}
    - query_results: {table_id: dict_de_retorno}
    - query_errors: {table_id: TabularError}
    """
    from app.evidence import tabular as tabular_mod

    tables_by_ref: dict = {}
    query_results: dict = {}
    query_errors: dict = {}
    query_calls: list = []  # records de cada chamada

    async def fake_find_by_urn(urn):
        return dict(tables_by_ref[urn]) if urn in tables_by_ref else None

    async def fake_find_by_id(table_id):
        for r in tables_by_ref.values():
            if r.get("id") == table_id:
                return dict(r)
        return None

    async def fake_execute(table_id, inputs=None, select=None, filters=None,
                           order_by=None, limit=100, executed_by=None,
                           interaction_id=None, agent_id=""):
        query_calls.append({
            "table_id": table_id, "inputs": inputs, "select": select,
            "filters": filters, "order_by": order_by, "limit": limit,
        })
        if table_id in query_errors:
            raise query_errors[table_id]
        return query_results.get(table_id, {
            "rows": [], "row_count": 0, "columns": [],
            "duration_ms": 1, "sql_rendered": "", "table": {"id": table_id},
        })

    # O engine importa lazy de app.evidence.tabular; patch direto no módulo
    monkeypatch.setattr(tabular_mod, "execute_query", fake_execute)
    monkeypatch.setattr("app.data_tables.queries.find_by_urn_with_ks", fake_find_by_urn)
    monkeypatch.setattr("app.data_tables.queries.find_by_id_with_ks", fake_find_by_id)

    return {
        "tables_by_ref": tables_by_ref,
        "query_results": query_results,
        "query_errors": query_errors,
        "query_calls": query_calls,
    }


def _make_skill_with_tables(tables: list[dict], bindings: list[dict] = None) -> ParsedSkill:
    return ParsedSkill(
        frontmatter=SkillFrontmatter(id="skill-test", version="1.0.0", execution_mode="declarative"),
        api_bindings_parsed=bindings or [],
        data_tables_parsed=tables,
    )


def _make_agent():
    return {"id": "agent-1", "name": "Agent X"}


def _run(coro):
    return asyncio.run(coro)


class TestExecuteDeclarativeWithDataTables:
    def test_skill_with_only_table_returns_success(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:abc:vendas:1"] = {
            "id": "t-1", "urn": "urn:table:abc:vendas:1", "name": "Vendas",
        }
        fake_tabular["query_results"]["t-1"] = {
            "rows": [{"valor": 100}, {"valor": 200}],
            "row_count": 2, "columns": ["valor"],
            "duration_ms": 5, "sql_rendered": "", "table": {"id": "t-1"},
        }
        skill = _make_skill_with_tables([{
            "id": "vendas_q4",
            "table_ref": "urn:table:abc:vendas:1",
            "query": {"select": ["valor"], "limit": 10},
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        assert result["final_state"] == "completed"
        # Context default: tables.<id>
        assert "tables" in result["context"]
        assert result["context"]["tables"]["vendas_q4"]["row_count"] == 2

    def test_inputs_template_resolved_in_filter_value(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:abc:vendas:1"] = {
            "id": "t-1", "urn": "urn:table:abc:vendas:1",
        }
        skill = _make_skill_with_tables([{
            "id": "v",
            "table_ref": "urn:table:abc:vendas:1",
            "query": {
                "filters": [{"col": "cliente_id", "op": "=", "value": "{{ inputs.cliente_id }}"}],
                "limit": 10,
            },
        }])
        _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill,
            inputs={"cliente_id": 42},
        ))
        # Verificar que o filtro chegou com value resolvido
        assert len(fake_tabular["query_calls"]) == 1
        call = fake_tabular["query_calls"][0]
        assert call["filters"][0]["value"] == 42

    def test_output_mapping_dict_alias_syntax(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:x:y:1"] = {
            "id": "t-2", "urn": "urn:table:x:y:1",
        }
        fake_tabular["query_results"]["t-2"] = {
            "rows": [{"nome": "Alice"}, {"nome": "Bob"}],
            "row_count": 2, "columns": ["nome"],
            "duration_ms": 1, "sql_rendered": "", "table": {"id": "t-2"},
        }
        skill = _make_skill_with_tables([{
            "id": "clientes",
            "table_ref": "urn:table:x:y:1",
            "query": {"select": ["nome"]},
            "output_mapping": {
                "lista_nomes": "$.rows[*].nome",
                "total": "$.row_count",
            },
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        # Context recebe aliases — não vai pra "tables.<id>"
        assert "tables" not in result["context"]
        assert result["context"]["lista_nomes"] == ["Alice", "Bob"]
        assert result["context"]["total"] == 2

    def test_table_not_found_with_on_error_fail(self, fake_repos, fake_tabular):
        # Não popula tables_by_ref → find retorna None
        skill = _make_skill_with_tables([{
            "id": "missing",
            "table_ref": "urn:table:nope:none:1",
            "on_error": "fail",
            "query": {"select": ["x"]},
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        assert result["final_state"] == "failed"
        assert any("não encontrada" in e for e in result["errors"])

    def test_table_not_found_with_on_error_continue(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:ok:t:1"] = {
            "id": "t-ok", "urn": "urn:table:ok:t:1",
        }
        fake_tabular["query_results"]["t-ok"] = {
            "rows": [{"x": 1}], "row_count": 1, "columns": ["x"],
            "duration_ms": 1, "sql_rendered": "", "table": {"id": "t-ok"},
        }
        skill = _make_skill_with_tables([
            {
                "id": "missing", "table_ref": "urn:table:nope:none:1",
                "on_error": "continue",
                "query": {"select": ["x"]},
            },
            {
                "id": "ok", "table_ref": "urn:table:ok:t:1",
                "query": {"select": ["x"]},
            },
        ])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        # Primeira falhou mas com continue → segunda rodou → completed/partial
        assert result["final_state"] in ("completed", "partial")
        # Ok tabela está no context
        assert result["context"]["tables"]["ok"]["row_count"] == 1

    def test_dry_run_skips_data_tables(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:x:y:1"] = {
            "id": "t-1", "urn": "urn:table:x:y:1",
        }
        skill = _make_skill_with_tables([{
            "id": "v", "table_ref": "urn:table:x:y:1",
            "query": {"select": ["x"]},
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={}, dry_run=True,
        ))
        assert result["dry_run"] is True
        # Nenhuma chamada ao service tabular
        assert fake_tabular["query_calls"] == []

    def test_binding_execution_audit_recorded(self, fake_repos, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:x:y:1"] = {
            "id": "t-1", "urn": "urn:table:x:y:1",
        }
        skill = _make_skill_with_tables([{
            "id": "v", "table_ref": "urn:table:x:y:1",
            "query": {"select": ["x"]},
        }])
        _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        # binding_executions tem uma row com binding_id="table:v"
        audits = list(fake_repos["binding_executions"].values())
        assert len(audits) == 1
        assert audits[0]["binding_id"] == "table:v"
        assert audits[0]["status_code"] == 200

    def test_missing_id_or_table_ref_reports_error(self, fake_repos, fake_tabular):
        skill = _make_skill_with_tables([{
            "id": "", "table_ref": "x",  # id vazio
            "query": {},
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        # data_tables_parsed só permite items com id+table_ref no parser,
        # mas aqui injetei diretamente — engine deve detectar.
        assert any("data_table inválido" in e for e in result["errors"])


class TestIfPresentFilters:
    """WHERE multi-campo (2026-06-10): filtros `if_present` de input AUSENTE são
    descartados ANTES do Jinja estrito — sem isso, "{{ inputs.X }}" com X ausente
    estourava StrictUndefined e derrubava a fase inteira."""

    def _table(self, fake_tabular):
        fake_tabular["tables_by_ref"]["urn:table:abc:t:1"] = {
            "id": "t-1", "urn": "urn:table:abc:t:1",
        }

    def _skill_multi(self):
        return _make_skill_with_tables([{
            "id": "q", "table_ref": "urn:table:abc:t:1",
            "query": {"filters": [
                {"col": "a", "op": "=", "value": "{{ inputs.a }}", "if_present": "a"},
                {"col": "b", "op": "=", "value": "{{ inputs.b }}", "if_present": "b"},
            ], "limit": 10},
        }])

    def test_absent_input_filter_dropped_before_render(self, fake_repos, fake_tabular):
        self._table(fake_tabular)
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=self._skill_multi(), inputs={"a": 1},
        ))
        assert result["final_state"] == "completed"   # sem StrictUndefined p/ b
        call = fake_tabular["query_calls"][0]
        assert [f["col"] for f in call["filters"]] == ["a"]   # filtro de b descartado
        assert call["filters"][0]["value"] == 1

    def test_both_inputs_both_filters_applied(self, fake_repos, fake_tabular):
        self._table(fake_tabular)
        _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=self._skill_multi(),
            inputs={"a": 1, "b": "RS"},
        ))
        call = fake_tabular["query_calls"][0]
        assert [f["col"] for f in call["filters"]] == ["a", "b"]

    def test_no_inputs_all_dropped_lists_all(self, fake_repos, fake_tabular):
        self._table(fake_tabular)
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=self._skill_multi(), inputs={},
        ))
        assert result["final_state"] == "completed"
        assert fake_tabular["query_calls"][0]["filters"] == []   # sem WHERE → lista até o limit

    def test_empty_string_input_treated_as_absent(self, fake_repos, fake_tabular):
        self._table(fake_tabular)
        _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=self._skill_multi(),
            inputs={"a": "", "b": "x"},
        ))
        assert [f["col"] for f in fake_tabular["query_calls"][0]["filters"]] == ["b"]

    def test_filter_without_if_present_applies_with_none(self, fake_repos, fake_tabular):
        """Sem if_present, input ausente NÃO derruba: expressão Jinja PURA
        ("{{ inputs.a }}") usa compile_expression (undefined→None) → o filtro
        vai ao serviço com value=None (`a = NULL` → 0 linhas). Comportamento
        legado SEGURO (não vaza dados) — era a causa das '0 linhas silenciosas'
        no chat; filtros opcionais devem usar if_present."""
        self._table(fake_tabular)
        skill = _make_skill_with_tables([{
            "id": "q", "table_ref": "urn:table:abc:t:1",
            "query": {"filters": [
                {"col": "a", "op": "=", "value": "{{ inputs.a }}"},
            ], "limit": 10},
        }])
        result = _run(de.execute_declarative(
            agent=_make_agent(), skill_parsed=skill, inputs={},
        ))
        assert result["final_state"] == "completed"
        call = fake_tabular["query_calls"][0]
        assert [f["col"] for f in call["filters"]] == ["a"]   # filtro mantido
        assert call["filters"][0]["value"] is None            # value=None → 0 linhas
