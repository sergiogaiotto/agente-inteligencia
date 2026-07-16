"""UI da depreciação per-tool em /mcp (39.x — item 3 PR5).

Asserts de string no template: Alpine só executa em browser, mas o payload
que o JS monta e os testids são texto — e é exatamente aí que moram os bugs
que este PR conserta (o tri-state que não persistia).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return Path("app/templates/pages/tools.html").read_text(encoding="utf-8")


class TestTriStatePersiste:
    def test_savetool_envia_per_tool_mode(self, html):
        """O <select> existe desde a 39.0.0 e o backend aceita o campo, mas o
        payload nunca o enviava: o operador escolhia "Ligado", salvava, e a
        escolha era descartada em silêncio — o modo por conector era
        inalcançável pela UI."""
        assert "per_tool_mode: String(this.form.per_tool_mode || 'inherit')" in html

    def test_form_de_criacao_inclui_per_tool_mode(self, html):
        """O branch de criação do openEditor não tinha o campo → o <select>
        abria com undefined e salvava sem modo."""
        criacao = html.split("this.editingId = null;")[1].split("this.authConfig")[0]
        assert "per_tool_mode:'inherit'" in criacao.replace(" ", "")


class TestBadgeLegado:
    def test_chip_presente_com_testid(self, html):
        assert 'data-testid="tool-legacy-chip"' in html
        assert 'x-show="isLegacy(tool.id)"' in html

    def test_hint_acionavel_no_title(self, html):
        assert ':title="legacyHint(tool.id)"' in html
        for motivo in ("global_off_herdando", "modo_off_explicito",
                       "nunca_descoberto", "backfill_nao_cobre_auth",
                       "sem_endpoint"):
            assert motivo in html, f"hint sem cobertura para {motivo}"

    def test_hint_de_quem_herda_nao_manda_escolher_herdar(self, html):
        """O caso que cobre a frota inteira por default (global OFF + Herdar):
        mandar "mude para Herdar" seria um no-op — o conector JÁ está lá."""
        i = html.find("global_off_herdando:")
        assert i > 0
        hint = html[i:html.find("\n", i)]
        assert "MCP_PER_TOOL_ENABLED" in hint or "global" in hint
        assert '"Ligado"' in hint

    def test_predicado_nao_e_reimplementado_em_js(self, html):
        """O modo efetivo compõe per_tool_mode com a flag GLOBAL, que não chega
        ao front. Computar em JS seria uma 2ª verdade, em outra linguagem, fora
        do alcance dos testes de paridade. (Citar o nome da flag num TEXTO de
        ajuda é outra coisa — é instrução para humano, não lógica.)"""
        script = html.split("{% block scripts %}")[-1]
        # O veredito vem pronto do servidor; o front só indexa por id.
        assert "legacyById[id]" in html
        for reimpl in ("per_tool_mode ===", "per_tool_mode==", "=== 'inherit'",
                       "== 'inherit'", "perToolEnabled"):
            assert reimpl not in script, f"tri-state reimplementado em JS: {reimpl}"


class TestPainelDeCobertura:
    def test_pagina_busca_a_metrica(self, html):
        assert "/api/v1/tools/per-tool-coverage" in html
        assert 'data-testid="per-tool-coverage"' in html

    def test_guard_de_amostra_pequena(self, html):
        """0/0 = 100% seria falsa confiança num gate."""
        assert 'x-if="coverage && coverage.scanned"' in html
        assert 'data-testid="coverage-empty"' in html

    def test_truncated_visivel(self, html):
        assert 'data-testid="coverage-truncated"' in html

    def test_mostra_adocao_ao_lado_da_prontidao(self, html):
        """Só a prontidão fazia a tela se autocontradizer no default: "100%"
        em verde com o chip "legado" em TODAS as linhas."""
        assert 'data-testid="coverage-legacy-count"' in html
        assert "em_legado_hoje" in html

    def test_erro_da_metrica_e_visivel(self, html):
        """Falhar calado zerava painel+chips+botão: "sem legado" e "métrica
        quebrada" ficavam indistinguíveis."""
        assert 'data-testid="coverage-error"' in html
        assert "coverageError" in html

    def test_botao_de_backfill(self, html):
        assert 'data-testid="btn-backfill-discovered"' in html
        assert "/api/v1/tools/backfill-discovered" in html

    def test_contagem_da_lista_admite_a_frota_maior(self, html):
        """A lista carrega 50; o painel varre a frota inteira — sem o "de N",
        duas contagens da MESMA tela se contradizem."""
        assert 'data-testid="tools-count"' in html
        assert "registradas" in html


class TestJinjaFootgun:
    def test_helpers_novos_estao_no_bloco_de_scripts(self, html):
        script = html.split("{% block scripts %}")[-1]
        for trecho in ("legacyById", "loadCoverage", "runBackfill", "legacyHint"):
            assert trecho in script, f"{trecho} fora do bloco de scripts"

    def test_sem_chaves_duplas_literais_novas_no_script(self, html):
        """`{{...}}` literal dentro de <script> é interpolado pelo Jinja ou dá
        500 no GET — pytest não pega, só um browser real."""
        import re
        script = html.split("{% block scripts %}")[-1]
        # Remove comentários de linha (onde `{operation, query}` aparece em prosa)
        sem_comentarios = re.sub(r"//[^\n]*", "", script)
        assert "{{" not in sem_comentarios, "chave dupla literal no <script>"
