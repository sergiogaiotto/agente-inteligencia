"""## Response Template — frase humana DETERMINÍSTICA (sem LLM) no declarativo.

Cobre: parser (extração + gate inalterado), engine (render contra inputs+context,
espelha output, fallbacks: sem template / jinja quebrado / dry_run / lenient),
e o default gerado pelo Wizard p/ skills de tabela.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents import declarative_engine as de
from app.agents.declarative_engine import _strip_md_fence
from app.skill_parser.parser import ParsedSkill, SkillFrontmatter, parse_skill_md


# ─── _strip_md_fence (puro) ──────────────────────────────────────


def test_strip_md_fence():
    assert _strip_md_fence("```jinja\nOlá {{ x }}\n```") == "Olá {{ x }}"
    assert _strip_md_fence("```\nfoo\n```") == "foo"
    assert _strip_md_fence("sem fence {{ x }}") == "sem fence {{ x }}"
    assert _strip_md_fence("  trimmed  ") == "trimmed"
    assert _strip_md_fence("") == ""


# ─── parser ──────────────────────────────────────────────────────


_REQUIRED = """## Purpose
p
## Activation Criteria
a
## Inputs
```json
{"type": "object"}
```
## Workflow
w
## Tool Bindings
sem tools
## Output Contract
o
## Failure Modes
f
"""


def _skill_md(extra: str, declarative: bool = True) -> str:
    em = "execution_mode: declarative\n" if declarative else ""
    return f"---\nid: urn:skill:x:subagent:t\nkind: subagent\n{em}---\n\n# T\n\n{_REQUIRED}\n{extra}"


def test_parser_extracts_response_template():
    md = _skill_md(
        "## Data Tables\n```yaml\ntables:\n  - id: v\n    table_ref: urn:table:a:b:1\n"
        "    query:\n      select: [x]\n```\n\n"
        "## Response Template\n```jinja\nResultado {{ context.tables }}\n```"
    )
    sk = parse_skill_md(md)
    assert "Resultado {{ context.tables }}" in sk.response_template
    assert sk.is_valid is True   # tem Data Tables → gate satisfeito


def test_response_template_alone_does_not_satisfy_gate():
    # declarative com SÓ ## Response Template (sem binding/table) → inválido
    md = _skill_md("## Response Template\nOlá {{ inputs.x }}")
    sk = parse_skill_md(md)
    assert sk.is_valid is False
    assert any("API Bindings" in e or "Data Tables" in e for e in sk.validation_errors)


# ─── fixtures de engine (in-memory, sem DB/DuckDB) ───────────────


class _FakeRepo:
    def __init__(self):
        self.store = {}

    async def find_by_id(self, id_):
        return dict(self.store[id_]) if id_ in self.store else None

    async def create(self, data):
        self.store[data["id"]] = dict(data)
        return data

    async def update(self, id_, data):
        if id_ in self.store:
            self.store[id_].update(data)
        return self.store.get(id_)


@pytest.fixture
def fake_repos(monkeypatch):
    monkeypatch.setattr(de, "interactions_repo", _FakeRepo())
    monkeypatch.setattr(de, "binding_executions_repo", _FakeRepo())
    monkeypatch.setattr(de, "api_call_logs_repo", _FakeRepo())


@pytest.fixture
def fake_tabular(monkeypatch):
    from app.evidence import tabular as tabular_mod
    tables: dict = {}
    results: dict = {}

    async def fake_find_by_urn(urn):
        return dict(tables[urn]) if urn in tables else None

    async def fake_find_by_id(tid):
        for r in tables.values():
            if r.get("id") == tid:
                return dict(r)
        return None

    async def fake_execute(table_id, **kw):
        return results.get(table_id, {"rows": [], "row_count": 0, "columns": [],
                                      "duration_ms": 1, "sql_rendered": "", "table": {"id": table_id}})

    monkeypatch.setattr(tabular_mod, "execute_query", fake_execute)
    monkeypatch.setattr("app.data_tables.queries.find_by_urn_with_ks", fake_find_by_urn)
    monkeypatch.setattr("app.data_tables.queries.find_by_id_with_ks", fake_find_by_id)
    return {"tables": tables, "results": results}


def _skill(response_template="", output_mapping=None):
    return ParsedSkill(
        frontmatter=SkillFrontmatter(id="s", execution_mode="declarative"),
        data_tables_parsed=[{
            "id": "lim",
            "table_ref": "urn:table:abc:credito:1",
            "query": {"select": ["cd_cliente", "vr_limite"]},
            **({"output_mapping": output_mapping} if output_mapping else {}),
        }],
        response_template=response_template,
    )


def _setup_table(fake_tabular, rows):
    fake_tabular["tables"]["urn:table:abc:credito:1"] = {"id": "t-1", "urn": "urn:table:abc:credito:1"}
    fake_tabular["results"]["t-1"] = {"rows": rows, "row_count": len(rows),
                                      "columns": ["cd_cliente", "vr_limite"],
                                      "duration_ms": 1, "sql_rendered": "", "table": {"id": "t-1"}}


def _run(skill, inputs, dry_run=False):
    return asyncio.run(de.execute_declarative(
        agent={"id": "a", "name": "A"}, skill_parsed=skill, inputs=inputs, dry_run=dry_run,
    ))


# ─── engine ──────────────────────────────────────────────────────


def test_engine_renders_template_and_mirrors_output(fake_repos, fake_tabular):
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    skill = _skill(
        response_template="O limite do cliente {{ inputs.cd }} é R$ {{ context.res[0].vr_limite }}.",
        output_mapping={"res": "$.rows"},
    )
    result = _run(skill, {"cd": 2})
    assert result["answer"] == "O limite do cliente 2 é R$ 1000."
    assert result["output"] == "O limite do cliente 2 é R$ 1000."   # espelhado


def test_engine_no_template_renders_default_table(fake_repos, fake_tabular):
    """Sem ## Response Template, o render DEFAULT mostra os DADOS (markdown)."""
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    result = _run(_skill(), {})   # sem response_template
    assert result["answer"] is not None
    assert "| cd_cliente | vr_limite |" in result["answer"]
    assert "| 2 | 1000 |" in result["answer"]
    assert "1 registro(s)" in result["answer"]
    assert result["output"] == result["answer"]        # espelhado
    # _table_meta é interno — não vaza p/ bindings_executed
    for b in result["bindings_executed"]:
        assert "_table_meta" not in b


def test_engine_bad_jinja_falls_back_to_default_table(fake_repos, fake_tabular):
    """Template com SyntaxError → cadeia de fallback cai no render default."""
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    skill = _skill(response_template="{% if %} quebrado", output_mapping={"res": "$.rows"})
    result = _run(skill, {})
    assert result["answer"] is not None         # sem crash
    assert "| 2 | 1000 |" in result["answer"]   # default mostra os dados


def test_engine_dry_run_skips_render(fake_repos, fake_tabular):
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    skill = _skill(response_template="Olá {{ inputs.x }}", output_mapping={"res": "$.rows"})
    result = _run(skill, {"x": 1}, dry_run=True)
    assert result["answer"] is None             # dry_run não renderiza


def test_engine_lenient_missing_context_key(fake_repos, fake_tabular):
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    skill = _skill(response_template="Valor {{ context.ausente }} fim", output_mapping={"res": "$.rows"})
    result = _run(skill, {})
    assert result["answer"] is not None         # lenient: chave ausente → vazio, não estoura
    assert "Valor" in result["answer"] and "fim" in result["answer"]


def test_engine_fence_in_template_is_stripped(fake_repos, fake_tabular):
    _setup_table(fake_tabular, [{"cd_cliente": 2, "vr_limite": 1000}])
    skill = _skill(
        response_template="```jinja\nLimite {{ context.res[0].vr_limite }}\n```",
        output_mapping={"res": "$.rows"},
    )
    result = _run(skill, {})
    assert result["answer"] == "Limite 1000"    # fence removido antes de renderizar


# ─── render default (unit) ───────────────────────────────────────


def test_default_table_answer_unit():
    from app.agents.declarative_engine import _default_table_answer

    # sem tabelas (ou só falhas) → None (caller cai no JSON legado)
    assert _default_table_answer([]) is None
    assert _default_table_answer([{"kind": "table", "status": 500}]) is None
    assert _default_table_answer([{"kind": "api", "status": 200}]) is None

    # 0 linhas → frase de nenhum registro (com nome da tabela)
    rec = {"kind": "table", "status": 200,
           "response_data": {"rows": [], "columns": ["a"]},
           "_table_meta": {"name": "TB_X", "catalog": {}}}
    out = _default_table_answer([rec])
    assert "Nenhum registro encontrado em TB_X" in out

    # cap de linhas com nota de truncamento
    rows = [{"a": i} for i in range(25)]
    rec2 = {"kind": "table", "status": 200,
            "response_data": {"rows": rows, "columns": ["a"]}, "_table_meta": {}}
    out2 = _default_table_answer([rec2])
    assert "25 registro(s)" in out2
    assert "mais 5 linha(s)" in out2

    # PII catalogada mascarada; não-catalogada PASSA (higiene Tier 1)
    cat = {"columns": [{"name": "cpf", "pii_category": "cpf", "source": "human"}]}
    rec3 = {"kind": "table", "status": 200,
            "response_data": {"rows": [{"cpf": "111", "x": "ok"}], "columns": ["cpf", "x"]},
            "_table_meta": {"name": "", "catalog": cat}}
    out3 = _default_table_answer([rec3])
    assert "[CPF]" in out3 and "111" not in out3 and "ok" in out3

    # célula com pipe/None tratadas (não quebra a tabela markdown)
    rec4 = {"kind": "table", "status": 200,
            "response_data": {"rows": [{"a": "x|y", "b": None}], "columns": ["a", "b"]},
            "_table_meta": {}}
    out4 = _default_table_answer([rec4])
    assert "x\\|y" in out4


# ─── wizard: sem template default (engine default cobre) ────────


def test_wizard_emits_default_render_guidance_not_template():
    from app.routes.wizard import WizardSkillRequest, _build_wizard_prompt
    bindings = {"mcp_tools": [], "rag_sources": [], "api_endpoints": [],
                "data_tables": [{"id": "t1", "name": "TB_CRED", "urn": "urn:table:a:b:1",
                                 "row_count": 48, "schema_summary": "cd_cliente:BIGINT",
                                 "columns": ["cd_cliente", "vr_limite"], "suggested_pk": "cd_cliente"}]}
    system, user = _build_wizard_prompt(WizardSkillRequest(description="x"), bindings, "standard")
    combined = system + "\n" + user
    # NÃO emite mais o template default só-contagem ("Encontrei N registro(s)")
    assert "Encontrei {{ rows | length }}" not in combined
    # orienta o LLM a NÃO gerar a seção (o render default do engine cobre)
    assert "NÃO gere a seção `## Response Template`" in combined
    # a secção continua registrada no parser p/ templates curados à mão
    from app.skill_parser.parser import _section_to_attr
    assert _section_to_attr("Response Template") == "response_template"
