"""Fase 3 do mesh — Simulador de roteamento no canvas + Frases-Prova pela aresta.

A feature "uau" do pacote QA E2E: digitar uma frase de cliente e ver as ARESTAS
ACENDEREM no canvas — determinístico, via o MESMO endpoint do editor de regra e
do gate de publicação (test-conditional), custo zero de tokens. Cada linha da
simulação pode virar Frase-Prova da aresta com um clique (o veredito observado
vira o expect). O menu da aresta ganha "Rodar Frases-Prova" — o veredito do
publish gate sem abrir o editor.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "pages" / "mesh_flow.html").read_text(encoding="utf-8")


class TestEntradas:
    def test_item_no_menu_do_no_gateado_por_saidas(self, html):
        """Só aparece em nós que têm saídas roteáveis — num Especialista
        terminal o item seria um beco sem saída."""
        i = html.index('data-testid="ctx-route-sim"')
        bloco = html[i - 260: i + 60]
        assert 'x-show="hasRoutableEdges(ctxMenu.node.id)"' in bloco
        assert "openRouteSim(ctxMenu.node)" in bloco

    def test_item_de_frases_no_menu_da_aresta_com_contagem(self, html):
        i = html.index('data-testid="ctx-run-phrases"')
        bloco = html[i - 260: i + 260]
        assert "edgePhraseCount(ctxMenu.edgeId) > 0" in bloco, "sem frases → sem item"
        assert "runEdgePhrases(ctxMenu.edgeId)" in bloco


class TestSimulacao:
    def _fn(self, html: str) -> str:
        i = html.index("async runRouteSim()")
        return html[i: i + 2400]

    def test_usa_o_endpoint_deterministico(self, html):
        i = html.index("async _testExpr(")
        fn = html[i: i + 400]
        assert "/api/v1/mesh/connections/test-conditional" in fn
        assert "r.result === true" in fn   # shape real: {result: bool}

    def test_default_dispara_so_sem_condicional_casada(self, html):
        """Semântica do runtime: default = else. Simular diferente mentiria."""
        fn = self._fn(html)
        assert "!anyConditionalHit" in fn

    def test_arestas_acendem_e_esmaecem(self, html):
        fn = self._fn(html)
        assert "this.simEdges = marks" in fn
        # e o renderEdges aplica o highlight
        i = html.index("if (this.simEdges) {")
        r = html[i: i + 300]
        assert "'hit'" in r and "op = 1" in r
        assert "'miss'" in r

    def test_guard_de_painel_fechado_durante_await(self, html):
        fn = self._fn(html)
        assert "this.routeSim !== rs" in fn

    def test_lente_fecha_o_simulador(self, html):
        """Trocar de pipeline muda as arestas visíveis — highlight órfão mente."""
        i = html.index("selectMesh() {")
        assert "closeRouteSim()" in html[i: i + 220]
        j = html.index("selectPipeline(p) {")
        assert "closeRouteSim()" in html[j: j + 400]


class TestFraseProvaPeloCanvas:
    def test_veredito_observado_vira_expect(self, html):
        i = html.index("async addSimPhrase(")
        fn = html[i: i + 1400]
        assert "expect: !!r.hit" in fn
        assert "where: 'input'" in fn

    def test_nao_duplica_frase(self, html):
        i = html.index("async addSimPhrase(")
        fn = html[i: i + 1400]
        assert "já é uma Frase-Prova" in fn

    def test_put_preserva_o_contrato_da_conexao(self, html):
        """PUT de conexão exige o corpo completo (contrato da API de mesh) —
        mandar só o config apagaria source/target/type."""
        i = html.index("async addSimPhrase(")
        fn = html[i: i + 1400]
        for campo in ("source_agent_id", "target_agent_id", "connection_type"):
            assert campo in fn


class TestRodarFrasesDaAresta:
    def _fn(self, html: str) -> str:
        i = html.index("async runEdgePhrases(")
        return html[i: i + 2200]

    def test_where_output_vai_como_output(self, html):
        """Frases de saída testam a RESPOSTA do agente — mandar como input
        inverteria o sentido do teste (mesmo contrato do editor)."""
        fn = self._fn(html)
        assert "{ output: p.text }" in fn
        assert "{ input: p.text }" in fn

    def test_veredito_e_pass_igual_expect(self, html):
        fn = self._fn(html)
        assert "matched === expected" in fn
        assert "p.expect !== false" in fn   # default expect=true (contrato da aresta)

    def test_resumo_honesto_no_toast(self, html):
        fn = self._fn(html)
        assert "há reprovas" in fn

    def test_le_o_proxy_reativo_de_volta(self, html):
        """Footgun Alpine: `this.routeSim = raw; const rs = raw` faz o guard
        `this.routeSim !== rs` (proxy !== raw) disparar cedo e descartar os
        resultados. Tem que ler o proxy de volta APÓS a atribuição."""
        fn = self._fn(html)
        i = fn.index("this.routeSim = {")
        depois = fn[i: i + 220]
        assert "const rs = this.routeSim" in depois, "rs precisa ser o proxy, não o objeto cru"
