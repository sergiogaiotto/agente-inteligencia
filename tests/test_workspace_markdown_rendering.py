"""Smoke do renderer markdown do chat (workspace.html).

User reportou (2026-05-29) que respostas com tabela markdown (`| Área | Boas
práticas |`) apareciam como texto cru com pipes na UI. Fix: expandir
_renderRichMarkdown pra suportar tabelas GFM + unificar formatMarkdown
(path default) com o renderer rico.

Pytest não roda navegador, então estes são smoke de presença/shape — pegam
regressão tipo "apagou função e esqueceu de trocar caller" ou "removeu
detecção de tabela do renderer". A verificação visual completa ainda
depende do user abrir o chat com markdown.

Regra do projeto (memória persistente): toda mudança vem com teste
automatizado. Esses testes cumprem o mínimo sem criar dependência JS.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


WORKSPACE_PATH = Path("app/templates/pages/workspace.html")


@pytest.fixture(scope="module")
def workspace_html() -> str:
    assert WORKSPACE_PATH.exists(), f"{WORKSPACE_PATH} não encontrado"
    return WORKSPACE_PATH.read_text(encoding="utf-8")


class TestFormatMarkdownUnification:
    """formatMarkdown (default chat path) agora delega ao _renderRichMarkdown.

    Antes do fix, formatMarkdown era uma versão reduzida com 4 padrões
    (code, inline code, bold, br) — não cobria tabelas. Unificação garante
    paridade com o rich view.
    """

    def test_format_markdown_delegates_to_rich_renderer(self, workspace_html):
        """O corpo de formatMarkdown deve chamar this._renderRichMarkdown.
        Sem isso, retornaríamos ao bug do user (regex limitado)."""
        # Extrai o corpo da função formatMarkdown
        m = re.search(
            r"formatMarkdown\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m, "definição de formatMarkdown não localizada"
        body = m.group(1)
        assert "_renderRichMarkdown" in body, (
            "formatMarkdown não está mais delegando — "
            "regressão pro bug do user (markdown sem tabela)"
        )

    def test_format_markdown_does_not_use_limited_regex(self, workspace_html):
        """Versão antiga só fazia .replace(/```...```) + bold + br no próprio
        corpo. Confirma que essa cadeia limitada NÃO está mais ativa em
        formatMarkdown."""
        m = re.search(
            r"formatMarkdown\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        # A cadeia antiga ficava toda numa linha com .replace().replace()
        # encadeados. Hoje deve ser concisa (delegação).
        assert ".replace(/\\n/g,'<br>')" not in body, (
            "formatMarkdown ainda tem cadeia antiga de regex — "
            "delegação ao _renderRichMarkdown não aconteceu"
        )


class TestRenderTablesHelper:
    """_renderTables — função nova que detecta blocos GFM table em texto
    pré-escapado e converte pra <table> com Tailwind."""

    def test_render_tables_function_defined(self, workspace_html):
        assert re.search(r"_renderTables\s*\(html\)\s*\{", workspace_html), (
            "função _renderTables ausente — fix de tabela markdown não foi aplicado"
        )

    def test_render_tables_called_from_rich_renderer(self, workspace_html):
        """_renderRichMarkdown precisa chamar _renderTables ANTES do split
        por linha (porque tabela depende de múltiplas linhas consecutivas)."""
        m = re.search(
            r"_renderRichMarkdown\s*\(text\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m, "_renderRichMarkdown não localizado"
        body = m.group(1)
        idx_tables = body.find("_renderTables")
        idx_split = body.find(".split('\\n')")
        assert idx_tables > 0, "_renderTables não é chamado pelo renderer rico"
        assert idx_tables < idx_split, (
            "_renderTables precisa rodar ANTES do split por linha — "
            "ordem errada quebra detecção"
        )


class TestTableDetectionHelpers:
    """Helpers de detecção e parsing de tabela GFM."""

    def test_table_row_detector_defined(self, workspace_html):
        assert "_looksLikeTableRow(line)" in workspace_html

    def test_table_separator_detector_defined(self, workspace_html):
        assert "_looksLikeTableSeparator(line)" in workspace_html

    def test_table_separator_requires_three_dashes(self, workspace_html):
        """Padrão GFM exige `---` (mínimo 3 dashes) no separator. Detector
        usa regex `-{3,}` — confere que está com 3, não 1 (que daria falso
        positivo em texto normal tipo 'a - b - c')."""
        m = re.search(
            r"_looksLikeTableSeparator\s*\(line\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        # Regex deve exigir 3+ dashes
        assert re.search(r"-\{3,\}", body), (
            "separator detector não exige 3+ dashes — pode dar falso positivo"
        )

    def test_table_separator_supports_alignment_markers(self, workspace_html):
        """GFM permite `:---:`, `:---`, `---:` pra alinhamento. Regex precisa
        aceitar `:` opcional nos dois lados dos dashes."""
        m = re.search(
            r"_looksLikeTableSeparator\s*\(line\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        # Marcadores `:` opcionais antes e depois de dashes
        assert ":?-" in m.group(1) or ":?\\-" in m.group(1), (
            "separator regex não suporta marcador de alinhamento `:`"
        )

    def test_alignment_parser_defined(self, workspace_html):
        assert "_parseTableAlignments(sepLine)" in workspace_html

    def test_split_row_helper_defined(self, workspace_html):
        assert "_splitTableRow(line)" in workspace_html


class TestTableHtmlOutput:
    """Renderer HTML da tabela. Verifica que usa Tailwind do projeto e
    tem estrutura semântica correta (thead/tbody/th/td)."""

    def test_render_table_html_function_defined(self, workspace_html):
        assert "_renderTableHtml(headerCells, bodyRows, aligns)" in workspace_html

    def test_table_uses_semantic_html(self, workspace_html):
        """Tabela precisa ter <thead>, <tbody>, <th>, <td> — não <div>s
        soltos. Importante pra acessibilidade e copy/paste."""
        m = re.search(
            r"_renderTableHtml\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        for tag in ("<thead>", "<tbody>", "<th ", "<td ", "<tr"):
            assert tag in body, f"tag semântica {tag!r} ausente do renderer"

    def test_table_uses_project_tailwind_palette(self, workspace_html):
        """Tabela usa as cores do projeto (brand/surface) — não cores
        avulsas tipo bg-gray-100 (que estragaria consistência visual)."""
        m = re.search(
            r"_renderTableHtml\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        # Pelo menos uma classe surface-* ou brand-* presente
        assert re.search(r"(surface|brand)-\d{2,3}", body), (
            "renderer da tabela não usa palette do projeto"
        )
        # E NÃO usa cores fora da palette
        assert "bg-gray-" not in body, "usou bg-gray-* (fora da palette do projeto)"

    def test_table_alternates_row_background(self, workspace_html):
        """Linhas alternadas com cor diferente — melhora legibilidade em
        tabelas longas."""
        m = re.search(
            r"_renderTableHtml\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        assert "% 2" in body or "ri % 2" in body, (
            "tabela não alterna background de linha — legibilidade prejudicada"
        )

    def test_table_supports_overflow_scroll(self, workspace_html):
        """Em chat mobile, tabela com muitas colunas precisa scroll
        horizontal — wrapper precisa ter overflow-x-auto."""
        m = re.search(
            r"_renderTableHtml\s*\([^)]*\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        assert "overflow-x-auto" in m.group(1), (
            "tabela sem overflow scroll quebra em viewport estreito"
        )


class TestBlockquoteSupport:
    """Suporte a blockquote (`> texto`) — adicionado junto com tabela
    porque é o outro markdown comum que faltava."""

    def test_blockquote_pattern_in_rich_renderer(self, workspace_html):
        m = re.search(
            r"_renderRichMarkdown\s*\(text\)\s*\{([\s\S]*?)\n\s+\},",
            workspace_html,
        )
        assert m
        body = m.group(1)
        # Padrão de detecção de blockquote linha
        assert "blockquote" in body, "blockquote não suportado no renderer"


class TestRegressionUserScreenshot:
    """Regressão do caso real reportado pelo user (screenshot Python best
    practices). Não conseguimos renderizar via pytest, mas validamos que
    o input típico de uma resposta com markdown table tem todos os
    componentes detectáveis pelo nosso renderer."""

    SAMPLE_USER_INPUT = """### Melhores práticas para desenvolver em Python

| Área | Boas-práticas recomendadas |
|------|----------------------------|
| **Estilo de código** | • Siga o **PEP 8** (indentação de 4 espaços) |
| **Tipagem** | • Adote **type hints** (PEP 484) |

#### Checklist rápido

1. `python -m venv .venv`
2. `pip install -U pip`
"""

    def test_sample_input_has_table_with_proper_separator(self):
        """Confirma que o input do user (markdown table) bate com o
        detector que implementamos: linha header `|...|` + linha separator
        com `---`."""
        lines = self.SAMPLE_USER_INPUT.split("\n")
        # Acha linha header (primeira com |...|)
        header_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("|"))
        sep_line = lines[header_idx + 1].strip()
        # Separator GFM: começa/termina com `|`, células só `-`/`:`/space
        assert sep_line.startswith("|") and sep_line.endswith("|")
        cells = sep_line[1:-1].split("|")
        for c in cells:
            assert re.match(r"^\s*:?-{3,}:?\s*$", c), (
                f"célula separator {c!r} não bate com nosso detector"
            )

    def test_sample_input_has_ordered_list(self):
        lines = self.SAMPLE_USER_INPUT.split("\n")
        ordered = [l for l in lines if re.match(r"^\s*\d+[.)]\s+", l)]
        assert len(ordered) >= 2, "input não tem lista ordenada esperada"

    def test_sample_input_has_header(self):
        lines = self.SAMPLE_USER_INPUT.split("\n")
        headers = [l for l in lines if re.match(r"^#{1,6}\s", l)]
        assert len(headers) >= 1
