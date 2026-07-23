"""Seletor compacto de bindings no Wizard (U1, 68.3.0).

A tela do wizard listava o CATÁLOGO inteiro de cada binding como chips
(42 fontes RAG tomavam a tela); agora mostra só os SELECIONADOS + busca
com autocomplete (macro Jinja `binding_picker` + estado local Alpine
`bindingPicker()`). Como os data-testid nascem no macro ({{ tipo }}),
os invariantes são checados no HTML RENDERIZADO — o que o browser vê —
não no fonte do template.

Invariantes selados:
- os 4 seletores existem com data-testid (harness E2E ancora neles);
- busca com teclado (↑↓/Enter/Esc) e painel com contador;
- os arrays de seleção e toggles do skillForm são os MESMOS (payload da
  geração intocado — a matriz bindings×verbosidade do #740 segue valendo);
- o sub-bloco min_relevance do RAG e o aviso de API vazia sobrevivem;
- o grid antigo (checkbox escondido POR ITEM do catálogo) saiu do wizard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader


@pytest.fixture(scope="module")
def wiz() -> str:
    """Região do WIZARD no HTML renderizado (o EDITOR, mais abaixo, ainda
    tem checkboxes próprios — U2 cuida dele)."""
    env = Environment(
        loader=FileSystemLoader("app/templates"), undefined=ChainableUndefined,
    )
    env.globals.update(app_version="test", request=None)
    html = env.get_template("pages/skill_form.html").render(skill_id="", role="root")
    return html[html.index("WIZARD IA PANEL"):html.index("Contrato de Decisão (## Decisions)")]


TIPOS = ("mcp", "api", "rag", "tabelas")


class TestBindingPickerWizard:
    def test_quatro_seletores_com_testid(self, wiz):
        for t in TIPOS:
            assert f'data-testid="binding-picker-{t}"' in wiz, t
            assert f'data-testid="binding-picker-{t}-input"' in wiz, t

    def test_busca_teclado_e_estado_local(self, wiz):
        for marcador in ("@keydown.down.prevent", "@keydown.up.prevent",
                         "@keydown.enter.prevent", "@keydown.escape.stop",
                         '@click.outside="open = false"'):
            assert marcador in wiz, marcador
        # estado local por seção (x-data aninhado enxerga o skillForm)
        assert wiz.count('x-data="bindingPicker(') == 4
        # helper global + busca acento-insensível (pt-BR) definidos no fonte
        src = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
        assert "function bindingPicker(fields)" in src
        assert "normalize('NFD')" in src

    def test_payload_intocado_arrays_e_toggles_originais(self, wiz):
        for arr, fn in (("wizardMcpIds", "toggleWizardMcp"),
                        ("wizardApiKeys", "toggleWizardApi"),
                        ("wizardSourceIds", "toggleWizardSource"),
                        ("wizardTableIds", "toggleWizardTable")):
            assert arr in wiz, arr
            assert fn in wiz, fn

    def test_chips_so_dos_selecionados_e_contador(self, wiz):
        # chips filtram o catálogo pela seleção (a inversão que resolve a tela)
        assert wiz.count(".filter(x => (") >= 4
        # contador N/total no cabeçalho de cada seção
        assert wiz.count(".length + '/' + (") >= 4

    def test_grid_antigo_de_catalogo_saiu_do_wizard(self, wiz):
        """O padrão antigo era um checkbox escondido POR ITEM do catálogo."""
        assert 'type="checkbox"' not in wiz

    def test_sub_blocos_preservados(self, wiz):
        # threshold de evidência continua condicionado a ter fonte marcada
        assert 'x-show="wizardSourceIds.length > 0"' in wiz
        assert "wizardMinRelevance" in wiz
        # aviso de "nenhum endpoint cadastrado" continua
        assert "Nenhum endpoint de API cadastrado." in wiz
        # links Gerenciar → das 4 seções
        assert wiz.count("Gerenciar →") >= 4

    def test_metadados_no_painel(self, wiz):
        # confidencialidade visível ANTES de vincular (restricted em vermelho)
        assert "'bg-red-100 text-red-700': it.confidentiality_label === 'restricted'" in wiz
        # linhas da tabela e método+conector da API
        assert "it.row_count" in wiz
        assert "it.method" in wiz and "it.conn_name" in wiz
