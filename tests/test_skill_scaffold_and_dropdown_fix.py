"""Dois ajustes de UI na tela "Nova Skill" (reportados no QA E2E 2026-07-16).

1. **Scaffold intocado validava verde** — a UI pré-preenche o editor com um
   modelo que já tem todas as seções obrigatórias, então clicar em Validar dizia
   "Markdown limpo" mesmo sendo só texto-guia. O parser agora detecta o modelo
   não-editado e marca `is_valid=False` com um erro claro.
2. **Dropdown "Inserir API" sumia sob a sidebar** — era o botão mais à esquerda
   e o painel (`right-0`, 384px) abria para a esquerda, invadindo a barra lateral
   que pinta por cima. Agora ancora `left-0` (abre para a direita, no conteúdo).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.skill_parser.parser import parse_skill_md


# Réplica fiel do `_newSkillScaffold()` de skill_form.html — o modelo que a UI
# pré-preenche ao criar uma nova skill.
_SCAFFOLD = "\n".join([
    "---",
    "id: urn:skill:dominio:tipo:slug",
    "version: 0.1.0",
    "kind: subagent",
    "owner: equipe",
    "stability: alpha",
    "---",
    "",
    "# Nome da Skill",
    "",
    "## Purpose",
    "Em 1-2 frases: o que esta skill faz e quando usá-la.",
    "",
    "## Activation Criteria",
    "- Quando acionar esta skill (gatilhos, intenções, tipos de pedido).",
    "",
    "## Inputs",
    "- entrada: descreva cada input esperado.",
    "",
    "## Workflow",
    "1. Passo a passo do que a skill executa.",
    "",
    "## Tool Bindings",
    '- Ferramentas/APIs/MCP que a skill usa (ou "nenhuma").',
    "",
    "## Output Contract",
    "- Formato e conteúdo da resposta esperada.",
    "",
    "## Failure Modes",
    "- O que fazer sem dado, em erro, ou fora do escopo.",
    "",
])

_SKILL_REAL = "\n".join([
    "---",
    "id: urn:skill:telecom:subagent:nexus-noc",
    "version: 0.1.0",
    "kind: subagent",
    "owner: qa-nexus",
    "stability: beta",
    "---",
    "",
    "# Nexus Telecom — Suporte Tecnico NOC",
    "",
    "## Purpose",
    "Diagnosticar e encaminhar incidentes tecnicos de internet da Nexus.",
    "",
    "## Activation Criteria",
    "- Cliente relata falha, lentidao ou interrupcao de servico",
    "",
    "## Inputs",
    "- cliente_id: identificador do cliente",
    "",
    "## Workflow",
    "1. **Confirme** o servico afetado e a abrangencia.",
    "",
    "## Tool Bindings",
    "- Nenhuma no piloto.",
    "",
    "## Output Contract",
    "- Resumo do diagnostico e proximo passo.",
    "",
    "## Failure Modes",
    "- Sem info do servico: perguntar qual servico esta com problema.",
    "",
])


def _scaffold_errors(parsed):
    return [e for e in parsed.validation_errors if "MODELO padrão" in e]


class TestScaffoldNaoValidaVerde:
    def test_scaffold_intocado_e_invalido(self):
        """O modelo pré-preenchido NÃO pode passar como 'Markdown limpo'."""
        p = parse_skill_md(_SCAFFOLD)
        assert p.is_valid is False
        assert _scaffold_errors(p), "scaffold intocado deveria disparar o erro de modelo"

    def test_skill_real_passa(self):
        """Uma skill de verdade (id/nome/guias substituídos) valida normalmente."""
        p = parse_skill_md(_SKILL_REAL)
        assert _scaffold_errors(p) == []
        assert p.is_valid is True

    def test_id_placeholder_sozinho_dispara(self):
        """Editou os textos mas deixou a URN placeholder → ainda flagra."""
        quase = _SKILL_REAL.replace(
            "urn:skill:telecom:subagent:nexus-noc", "urn:skill:dominio:tipo:slug")
        p = parse_skill_md(quase)
        assert _scaffold_errors(p), "URN placeholder deveria flagrar mesmo com corpo real"

    def test_nome_placeholder_sozinho_dispara(self):
        quase = _SKILL_REAL.replace(
            "# Nexus Telecom — Suporte Tecnico NOC", "# Nome da Skill")
        assert _scaffold_errors(parse_skill_md(quase))

    def test_edicao_parcial_com_guias_residuais_flagra(self):
        """id/nome trocados mas >=2 frases-guia ainda no corpo → não-publicável."""
        parcial = _SCAFFOLD.replace(
            "id: urn:skill:dominio:tipo:slug", "id: urn:skill:x:subagent:y"
        ).replace("# Nome da Skill", "# Minha Skill")
        p = parse_skill_md(parcial)
        assert _scaffold_errors(p)


class TestDropdownApiNaoSomeSobSidebar:
    @pytest.fixture(scope="module")
    def html(self) -> str:
        return (Path(__file__).resolve().parent.parent / "app" / "templates"
                / "pages" / "skill_form.html").read_text(encoding="utf-8")

    def test_dropdown_api_ancora_left_para_abrir_no_conteudo(self, html):
        i = html.find('x-show="apiDropdown"')
        assert i > 0
        painel = html[i:i + 400]
        assert "left-0 top-full" in painel, "dropdown API deve abrir à direita (left-0)"
        assert "right-0 top-full" not in painel, "right-0 fazia o painel sumir sob a sidebar"
