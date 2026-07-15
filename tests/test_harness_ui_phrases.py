"""UI fundida do harness (36.7.0 — itens 1-PR3 + 5-PR2 do plano).

Pina no SOURCE dos templates (padrão da suíte p/ UI, cf.
test_drift_events_writer.test_endpoint_filtra_por_alvo) os marcadores da
entrega: Frases-Prova no drawer do run e no compare, rótulo de ALVO nos
seletores A/B, painel Baseline-por-alvo com critério visível, guards de
amostra pequena (convenção sem-falsa-confiança) e o rodapé do editor de
regras prometendo o ciclo contínuo (agora real).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def harness_src() -> str:
    return Path("app/templates/pages/harness.html").read_text(encoding="utf-8")


class TestFrasesProvaNaUI:
    def test_secao_no_drawer_do_run(self, harness_src):
        assert 'data-testid="run-routing-phrases"' in harness_src
        assert "dimension_breakdown?.routing_phrases" in harness_src
        # detalhe derrubado pela guarda anti-corrupção é comunicado, não mudo
        assert "failing_dropped" in harness_src
        # honestidade da métrica: prova a REGRA, não o LLM
        assert "não o comportamento do LLM" in harness_src

    def test_linha_no_compare_com_guarda(self, harness_src):
        assert 'data-testid="compare-routing-phrases"' in harness_src
        assert "compareResult.routing_phrases.comparable" in harness_src
        # incomparável mostra o reason do backend (hash divergente / N/A)
        assert "compareResult.routing_phrases.reason" in harness_src


class TestAlvoNoCompare:
    def test_seletores_ab_com_rotulo_de_alvo(self, harness_src):
        # ambos os selects usam targetLabel(r) no texto da option
        assert harness_src.count("targetLabel(r) || 'sem alvo'") >= 2

    def test_cards_ab_mostram_o_alvo(self, harness_src):
        assert "targetLabel(compareResult['run_'+side])" in harness_src


class TestBaselinePorAlvo:
    def test_painel_existe_com_criterio_visivel(self, harness_src):
        assert 'data-testid="baseline-by-target"' in harness_src
        # critério do baseline implícito DECLARADO na tela (sem falsa confiança)
        assert "recência" in harness_src
        assert "baselinesByTarget()" in harness_src

    def test_agrupamento_exclui_legado_e_usa_recencia(self, harness_src):
        assert "if (!r.pipeline_id && !r.agent_id) continue;" in harness_src
        assert "r.run_type !== 'baseline' || r.status !== 'completed'" in harness_src

    def test_lista_carrega_50_runs(self, harness_src):
        # com o default (20) o baseline de alvo pouco rodado sumiria do painel
        assert "/api/v1/eval-runs?limit=50" in harness_src


class TestGuardsDeAmostra:
    def test_badge_amostra_pequena_no_run(self, harness_src):
        assert "amostra pequena" in harness_src
        assert "(r.total_cases||0) < 5" in harness_src

    def test_refusal_rate_100_avisa_vacuo(self, harness_src):
        assert "pode ser vácuo" in harness_src
        assert "r.correct_refusal_rate===1" in harness_src


class TestRodapeDoEditor:
    def test_promessa_do_ciclo_continuo(self):
        src = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
        assert "avaliação do pipeline no Harness" in src
        assert "o ciclo vigia para sempre" in src
