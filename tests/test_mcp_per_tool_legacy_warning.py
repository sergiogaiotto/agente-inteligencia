"""Depreciação visível na SKILL (39.x — item 3 PR5).

Conector(es) em per-tool efetivo + Workflow citando `operation=` → o Workflow
fala a língua do caminho LEGADO, que o runtime não vai praticar. Warning (não
critical): é orientação de migração, não defeito.

Mora no `wizard_validator` e não no `linter` por necessidade estrutural: o
linter é síncrono e só vê o TEXTO da skill — `per_tool_enabled_for` precisa de
`per_tool_mode` e `_parse_discovered_tools` de `discovered_tools`, ambas
colunas do banco. Além disso o linter não tem consumidor de UI.
"""
from __future__ import annotations

import json

import pytest

from app.skill_parser.wizard_validator import validate_generated_skill
from app.skill_parser.parser import parse_skill_md


_DISC = json.dumps([{"name": "web_search", "inputSchema": {"type": "object"}}])


def _tool(**over):
    base = {"id": "t1", "name": "Tavily", "operations": "search",
            "discovered_tools": _DISC, "per_tool_mode": "on",
            "mcp_server": "http://mcp:3001"}
    base.update(over)
    return base


_SKILL = (
    "---\nid: urn:skill:x\nversion: 0.1.0\nkind: subagent\n---\n# S\n"
    "## Purpose\nBuscar coisas.\n"
    "## Activation Criteria\n- Quando pedirem busca\n"
    "## Workflow\n1. **Chame** a tool com operation=search e query=<termo>.\n"
    "## Tool Bindings\n- `Tavily`\n"
    "## Output Contract\n- Texto\n"
)

_SKILL_SEM_OPERATION = _SKILL.replace(
    "1. **Chame** a tool com operation=search e query=<termo>.",
    "1. **Chame** a ferramenta `web_search` com o termo do usuário.",
)


def _validate(skill_md, tools, completo=True):
    return validate_generated_skill(
        parse_skill_md(skill_md), bindings={"mcp_tools": tools},
        bindings_complete=completo,
    )


def _rules(res):
    return [v.rule for v in res.violations]


class TestWarningDispara:
    def test_quando_todos_avaliados_sao_per_tool_e_ha_citacao(self):
        res = _validate(_SKILL, [_tool()])
        assert "per_tool.legacy_operation_citation" in _rules(res)
        v = next(v for v in res.violations
                 if v.rule == "per_tool.legacy_operation_citation")
        assert v.severity == "warning"
        assert "search" in v.message

    def test_nao_derruba_ok_nem_dispara_retry(self):
        """Warning é orientação — reprovar a skill por causa dela seria bloquear
        migração de quem ainda nem migrou."""
        res = _validate(_SKILL, [_tool()])
        assert res.ok is True
        assert res.warning_count >= 1


class TestFrotaMistaNaoLevaConselhoQuebrado:
    """O achado mais perigoso da revisão adversarial deste PR.

    O Workflow é um texto ÚNICO e a extração de `operation=` varre tudo, sem
    escopo por conector. Numa skill com A (per-tool) + B (legado), o dry-run de
    A via só A, concluía "não sobrou legado" e mandava remover as citações
    `operation=` — que eram legítimas de B. Seguir a sugestão QUEBRARIA B.
    """

    def test_dryrun_de_um_conector_nao_opina_sobre_o_conjunto(self):
        # Só A é passado (é o que o dry-run faz), mas a skill tem B legado.
        res = _validate(_SKILL, [_tool()], completo=False)
        assert "per_tool.legacy_operation_citation" not in _rules(res)

    def test_wizard_com_a_skill_inteira_opina(self):
        res = _validate(_SKILL, [_tool()], completo=True)
        assert "per_tool.legacy_operation_citation" in _rules(res)

    def test_default_e_completo_para_nao_quebrar_callers(self):
        res = validate_generated_skill(parse_skill_md(_SKILL),
                                       bindings={"mcp_tools": [_tool()]})
        assert "per_tool.legacy_operation_citation" in _rules(res)

    def test_mensagem_afirma_o_conjunto_porque_agora_o_ve(self):
        res = _validate(_SKILL, [_tool()])
        msg = next(v.message for v in res.violations
                   if v.rule == "per_tool.legacy_operation_citation")
        assert "Todos os conectores desta skill" in msg


class TestWarningNaoDispara:
    def test_quando_ha_conector_legado_na_mistura(self):
        res = _validate(_SKILL, [_tool(), _tool(id="t2", per_tool_mode="off")])
        assert "per_tool.legacy_operation_citation" not in _rules(res)

    def test_quando_workflow_nao_cita_operation(self):
        res = _validate(_SKILL_SEM_OPERATION, [_tool()])
        assert "per_tool.legacy_operation_citation" not in _rules(res)

    def test_quando_conector_per_tool_nao_tem_descoberta(self, monkeypatch):
        """Sem descoberta ele CAI no legado — a citação `operation=` está certa."""
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "1")
        res = _validate(_SKILL, [_tool(discovered_tools=None)])
        assert "per_tool.legacy_operation_citation" not in _rules(res)

    def test_sem_tools_mcp(self):
        res = validate_generated_skill(parse_skill_md(_SKILL), bindings={"mcp_tools": []})
        assert "per_tool.legacy_operation_citation" not in _rules(res)


class TestRegrasLegadasIntactas:
    def test_operation_invented_segue_valendo_no_legado(self, monkeypatch):
        monkeypatch.setenv("MCP_PER_TOOL_ENABLED", "0")
        skill = _SKILL.replace("operation=search", "operation=inventada")
        res = _validate(skill, [_tool(per_tool_mode="off")])
        assert "operation.invented" in _rules(res)
        assert res.ok is False

    def test_per_tool_nao_dispara_operation_invented(self):
        """As 3 regras operation.* não rodam em skill só-per-tool (39.2.0) —
        o nome real da função não é uma "operation" a validar."""
        skill = _SKILL.replace("operation=search", "operation=inventada")
        res = _validate(skill, [_tool()])
        assert "operation.invented" not in _rules(res)
        assert "per_tool.legacy_operation_citation" in _rules(res)


class TestSuperficieDeUI:
    @pytest.mark.asyncio
    async def test_dryrun_cala_o_aviso_mas_mostra_a_funcao_real(self, monkeypatch):
        """O dry-run vê UM conector — não pode concluir nada sobre o conjunto.
        Ele não fica mudo: `per_tool.simulated` já nomeia a ferramenta real,
        que é a orientação concreta. Quem opina sobre o conjunto é o wizard."""
        import app.routes.skill_dryrun as dr
        tid = "11111111-1111-1111-1111-111111111111"

        async def _resolve(tool_id):
            return _tool(id=tool_id)
        monkeypatch.setattr(dr, "_resolve_tool_from_registry", _resolve)

        skill = _SKILL.replace("- `Tavily`", f"- `{tid}` (Tavily)")
        res = await dr.dry_run_tool(dr.DryRunRequest(skill_md=skill, tool_id=tid))
        rules = [i.rule for i in res.issues]
        assert "per_tool.legacy_operation_citation" not in rules
        assert "per_tool.simulated" in rules
        assert res.ok is True

    def test_wizard_passa_a_skill_inteira(self):
        """O wizard monta bindings com TODOS os conectores → default completo."""
        import inspect
        from app.routes import wizard
        src = inspect.getsource(wizard)
        assert "validate_generated_skill" in src
        # não passa bindings_complete=False em lugar nenhum
        assert "bindings_complete=False" not in src

    def test_dryrun_declara_incompleto(self):
        import inspect
        import app.routes.skill_dryrun as dr
        assert "bindings_complete=False" in inspect.getsource(dr._diagnose)
