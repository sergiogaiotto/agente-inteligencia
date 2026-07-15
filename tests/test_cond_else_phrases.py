"""Cond-A (35.17.0): "Tudo o mais" (else nativo descoberto) + Frases-Prova.

- O else nativo (connection_type='default') JÁ existe no motor; o problema é
  DESCOBERTA — o autor do Atlas escreveu not(...) com 20 cláusulas sem saber. O
  detector do not() manual oferece virar 'default' (que o motor mantém em sincronia).
- Frases-Prova: exemplos que a regra deve acertar, testados ao vivo contra o mesmo
  endpoint do simulador e SELADOS no config da aresta (test_phrases) — o runtime
  ignora essa chave (só lê expr), então é aditivo e sem regressão de roteamento.
"""
import json
from pathlib import Path

from app.agents.engine import _eval_conditional, _build_conditional_context

PG = Path("app/templates/pages/mesh_flow.html")


def test_frases_prova_estado_e_persistencia():
    src = PG.read_text(encoding="utf-8")
    # estado por aresta + hidratação a partir de cfg.test_phrases
    assert "testPhrases:" in src and "cfg.test_phrases" in src
    # buildConfig sela SÓ o contrato {text, where, expect} em conditional
    assert "cfg.test_phrases = ed.testPhrases" in src
    assert "expect: p.expect !== false" in src
    # achado do review: o select 'expect' precisa de .boolean, senão grava STRING
    # "false" e "deve pular" quebra (veredito invertido + selo errado + round-trip)
    assert 'x-model.boolean="p.expect"' in src
    # métodos: adicionar/remover/rodar + contador
    for m in ("addPhrase()", "removePhrase(i)", "async runPhrases()", "phrasesPassCount()"):
        assert m in src, f"falta {m}"
    # roda contra o MESMO endpoint do simulador
    assert "/api/v1/mesh/connections/test-conditional" in src
    # UI presente
    assert 'data-testid="phrases-section"' in src
    assert 'data-testid="phrase-add"' in src


def test_detector_do_senao_manual():
    src = PG.read_text(encoding="utf-8")
    # detector: conditional + expr not(...) + tem irmã condicional
    assert "looksLikeManualElse()" in src
    assert "/^\\s*not\\s*\\(/i.test" in src
    assert "convertToElse()" in src
    # converte para o tipo default nativo (o motor mantém em sincronia)
    assert "this.editor.type = 'default'" in src
    assert 'data-testid="else-hint"' in src
    assert 'data-testid="else-convert"' in src


def test_veredito_nao_mente_apos_editar_regra():
    """Ao mudar a regra, os vereditos ✓/✗ antigos são invalidados (não mentem).
    35.19.3: textarea delega a exprTyped (invalida + debounce de re-run) e o
    syncExpr da galeria delega ao helper único _exprMutated."""
    src = PG.read_text(encoding="utf-8")
    assert '@input="exprTyped()"' in src                      # textarea manual
    assert "this._phrasesDebounce = setTimeout" in src        # re-run com debounce, sem HTTP por tecla
    body = src.split("syncExpr() {", 1)[1][:400]
    assert "this._exprMutated()" in body                      # syncExpr galeria → helper único


def test_runphrases_tem_guarda_de_epoca():
    """Achado do review: um loop de runPhrases em voo NÃO pode sobrescrever o
    veredito de uma regra que foi editada no meio (senão mostra ✓ da regra velha)."""
    src = PG.read_text(encoding="utf-8")
    assert "this._phrasesGen" in src  # contador de época
    # aborta se a época mudou OU a expr corrente difere da capturada
    assert "gen !== this._phrasesGen" in src
    assert "(this.editor.expr || '').trim() !== expr" in src


def test_runtime_ignora_test_phrases_no_config():
    """test_phrases é chave EXTRA no config — o gate lê só 'expr'. Aditivo, sem
    regressão: a mesma expr avalia idêntico com ou sem as frases no config."""
    cfg = {"expr": "'pix' in output_lower",
           "test_phrases": [{"text": "quero fazer um pix", "where": "input", "expect": True}]}
    # o runtime desserializa e usa cfg['expr'] — as frases não entram na avaliação
    parsed = json.loads(json.dumps(cfg))
    ctx = _build_conditional_context(output="seu PIX foi enviado")
    assert _eval_conditional(parsed["expr"], ctx) is True
    # a chave extra não afeta nem quebra nada
    assert "test_phrases" in parsed and parsed["expr"] == "'pix' in output_lower"


# ─── "veredito não mente" COMPLETO (major do review do #619, 2026-07-15) ─────

def _src():
    from pathlib import Path
    return Path("app/templates/pages/mesh_flow.html").read_text(encoding="utf-8")


def test_expr_mutated_helper_nos_cinco_caminhos():
    """Mutação programática de editor.expr (x-model não dispara @input) tem que
    invalidar os ✓/✗ das Frases-Prova — senão o badge verde prova a regra
    ANTIGA na hora de selar. Helper único nos 5 caminhos + fixLit."""
    src = _src()
    assert "_exprMutated() {" in src
    # insertVar, fixVar, useSuggestion, backToGallery, convertToElse + fixLit + syncExpr
    assert src.count("this._exprMutated()") >= 7
    # o helper re-TESTA além de invalidar (senão o painel fica em '·' até o
    # autor adivinhar o workaround — review pré-push)
    helper_body = src.split("_exprMutated() {", 1)[1][:600]
    assert "this.runPhrases()" in helper_body
    # nenhum dos caminhos ficou com o reset RASO antigo — âncora na DEFINIÇÃO
    # (`fn {`), janela CURTA para não pegar carona no call da função seguinte
    # (review pré-push: 900 chars deixavam backToGallery passar via insertVar)
    for fn in ("insertVar(name) {", "fixVar(w) {", "useSuggestion() {", "backToGallery() {", "convertToElse() {"):
        body = src.split(fn, 1)[1][:400]
        assert "this._exprMutated()" in body, f"{fn} sem _exprMutated"


def test_hint_do_else_nao_aparece_com_irmao_default():
    """Dois defaults no mesmo nó RODAM ambos quando nada casa (gate por-conexão)
    — o hint 'Virar Tudo o mais' não pode aparecer se o nó já tem default."""
    src = _src()
    body = src.split("looksLikeManualElse() {", 1)[1][:800]
    assert "e.type === 'default'" in body
    assert "return false" in body


def test_rodape_promete_so_o_que_existe():
    """Lição do review do #619: o rodapé não pode prometer o que o backend não
    faz. Em 35.19.3 removemos a promessa aspiracional; em 36.0.0 ela virou REAL
    (gate de publicação roda as frases — evaluate_pipeline_test_phrases), então
    a promessa volta ACOMPANHADA do lastro."""
    src = _src()
    assert "rodam aqui no simulador sempre que a regra muda" in src
    # a promessa de regressão existe E cita o comportamento real (bloqueio)
    assert "teste de regressão do roteamento — reprovação bloqueia a publicação" in src


def test_convert_to_else_avisa_descarte_das_frases():
    """buildConfig só sela test_phrases em conditional — virar 'Tudo o mais'
    descartava as frases em silêncio ao salvar (minor do review)."""
    src = _src()
    body = src.split("convertToElse() {", 1)[1][:900]
    assert "serão descartadas ao salvar" in body
    assert "nPhrases" in body
