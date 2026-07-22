"""67.2.0 — Lente de pipeline abre ORGANIZADA (auto-layout efêmero do subgrafo).

Pedido do dono do produto (prints 1×2): ao selecionar um pipeline, os membros
herdavam as posições do Mesh completo (espalhados entre ~130 agentes) e o
subgrafo abria ilegível (~47% de zoom). Contrato selado aqui:

- selectPipeline → reset ao layout salvo + _lensLayout() (colunas por
  profundidade a partir da entrada da lente);
- o layout da lente é EFÊMERO: _lensLayout NUNCA chama _syncPositions/queueSave
  — as posições persistidas pertencem ao Mesh completo;
- sair da lente (selectMesh) restaura o layout salvo via _resolvePositions;
- arrastar nó DENTRO da lente não persiste (senão salvaria coordenadas da
  lente por cima do layout do mesh inteiro);
- o botão Auto-organizar na lente reorganiza SÓ a vista (early-return antes do
  caminho que salva);
- rebuild com lente ativa (conectar/incluir agente → reload) reaplica o layout.

Testes de template leem o ARQUIVO CRU (padrão da casa — pytest não executa JS).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "app" / "templates" / "pages" /
       "mesh_flow.html").read_text(encoding="utf-8")


def _method_body(header: str, closer: str = "\n        },") -> str:
    i = SRC.index(header)
    return SRC[i:SRC.index(closer, i)]


class TestLenteAbreOrganizada:
    def test_select_pipeline_reseta_e_aplica_lens_layout(self):
        body = _method_body("selectPipeline(p) {")
        assert "this._resolvePositions()" in body
        assert "this._lensLayout()" in body
        # ordem: reset ANTES do layout (limpa resquício da lente anterior).
        # Comparar pelas CHAMADAS ("this.…()") — comentários citam os nomes.
        assert body.index("this._resolvePositions()") < body.index("this._lensLayout()")

    def test_lens_layout_e_efemero_nunca_persiste(self):
        # Pelas CHAMADAS: o comentário do método cita os nomes de propósito.
        body = _method_body("_lensLayout() {")
        assert "this._syncPositions()" not in body
        assert "this.queueSave()" not in body

    def test_lens_layout_usa_entrada_explicita_e_bfs(self):
        # Mesma regra do lensStart: entry_agent_id tem prioridade; senão raiz
        # topológica; fora-da-cadeia vai para coluna final (não some).
        body = _method_body("_lensLayout() {")
        assert "entry_agent_id" in body
        assert "maxD + 1" in body


class TestSairDaLenteRestaura:
    def test_select_mesh_restaura_layout_salvo(self):
        i = SRC.index("selectMesh() {")
        linha = SRC[i:SRC.index("},", i)]
        assert "_resolvePositions()" in linha


class TestNadaDaLentePersiste:
    def test_drag_na_lente_nao_salva(self):
        i = SRC.index("if (this._drag.moved) {")
        bloco = SRC[i:i + 600]
        assert "if (!this.selectedPipeline) { this._syncPositions(); this.queueSave(); }" in bloco

    def test_auto_organizar_na_lente_nao_salva(self):
        body = _method_body("autoLayout() {")
        # o ramo da lente retorna ANTES do caminho que persiste
        assert body.index("if (this.selectedPipeline)") < body.index("queueSave")
        lens_branch = body[body.index("if (this.selectedPipeline)"):body.index("this._computeAutoLayout()")]
        assert "return;" in lens_branch and "queueSave" not in lens_branch


class TestRebuildComLenteAtiva:
    def test_build_reaplica_layout_da_lente(self):
        body = _method_body("build() {")
        assert "if (this.selectedPipeline) this._lensLayout();" in body
