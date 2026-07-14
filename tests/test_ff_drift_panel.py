"""FF5 (35.5.0) — painel de drift no /quality + miúdos de mesh.

O drift_events tinha writer (33.11.0) e alvo (35.1.0), mas o operador só via
um CONTADOR no /observability — o painel dá olhos aos eventos (severidade,
métrica, baseline→atual, alvo resolvido, estado vazio honesto). Miúdos:
tooltip do 'evid' explica os ramos HEURÍSTICOS (anexo/tool = 0,8 fixo ≠ RAG
score real — achado da bateria Pulsar) e testids genéricos no mesh (nó/aresta)
p/ E2E sem calibração de coordenadas.
"""
from __future__ import annotations

from pathlib import Path


class TestPainelDeDrift:
    def _html(self):
        return Path("app/templates/pages/quality.html").read_text(encoding="utf-8")

    def test_painel_presente_com_testid(self):
        html = self._html()
        assert 'data-testid="quality-drift-panel"' in html
        assert "Drift entre execuções do harness" in html

    def test_consome_o_endpoint_com_alvo(self):
        html = self._html()
        assert "/api/v1/dashboard/drift-events" in html
        assert "loadDrift" in html

    def test_alvo_resolvido_e_evento_antigo_honesto(self):
        html = self._html()
        # resolve nome via combos já carregados (zero fetch extra)
        assert "driftTargetLabel" in html
        assert "alvo não registrado (evento antigo)" in html

    def test_estado_vazio_ensina_a_acumular_historico(self):
        html = self._html()
        assert "do mesmo alvo" in html
        assert 'href="/harness"' in html

    def test_severidades_sem_roxo(self):
        # paleta do repo proíbe roxo/violet; severidade usa rose/amber/emerald
        html = self._html()
        seg = html[html.index("quality-drift-panel"):html.index("Explorador de alucinações")]
        assert "rose" in seg and "amber" in seg and "emerald" in seg
        assert "purple" not in seg and "violet" not in seg

    def test_metricas_do_writer_tem_rotulo(self):
        """Toda métrica que o writer emite tem rótulo pt-BR no painel."""
        from app.harness.evaluator import _DRIFT_METRICS
        html = self._html()
        for metric, _ in _DRIFT_METRICS:
            assert f"{metric}:" in html, f"métrica {metric} sem rótulo no driftMetricLabel"


class TestMiudosMesh:
    def test_tooltip_evid_explica_ramos_heuristicos(self):
        html = Path("app/templates/pages/mesh_playground.html").read_text(encoding="utf-8")
        assert "0,8 FIXO" in html          # anexo/tool sem RAG
        assert "pontuação real de recuperação" in html  # com RAG

    def test_testids_de_no_e_aresta(self):
        html = Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")
        assert 'data-testid="mesh-node"' in html
        assert 'data-testid="mesh-edge"' in html
        assert 'data-testid="node-port"' in html  # pré-existente (regressão)

    def test_nota_hnsw_no_troubleshooting(self):
        doc = Path("docs/troubleshooting.md").read_text(encoding="utf-8")
        assert "hnsw.ef_search" in doc
        assert "iterative_scan" in doc
