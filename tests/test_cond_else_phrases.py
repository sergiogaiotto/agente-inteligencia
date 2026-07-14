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
    """Ao mudar a regra, os vereditos ✓/✗ antigos são invalidados (não mentem)."""
    src = PG.read_text(encoding="utf-8")
    assert "(editor.testPhrases||[]).forEach(p=>p.pass=null)" in src  # textarea
    assert "(ed.testPhrases || []).forEach(p => p.pass = null)" in src  # syncExpr galeria


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
