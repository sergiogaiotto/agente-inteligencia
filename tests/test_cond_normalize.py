"""Normalização pt-BR das variáveis condicionais (Cond-B, 35.17.0).

A dor #1 dos fluxos reais: o autor digita a MESMA palavra duas vezes (com e sem
acento) porque o match é literal — 'não reconheço' or 'nao reconheco'. As vars
input_norm/output_norm/text_norm (lowercase + SEM acento) matam isso, e o card
de palavra-chave passa a gerar com elas por default. ADITIVAS: as exprs
existentes (sobre *_lower) seguem byte-idênticas — reprodutibilidade preservada.
"""
from pathlib import Path

from app.agents.engine import (
    _build_conditional_context, _eval_conditional, _strip_accents,
    _OUTPUT_CLASS_VARS, CONDITIONAL_VARS_META, _expr_uses_output,
)

PG = Path("app/templates/pages/mesh_flow.html")


def test_strip_accents():
    assert _strip_accents("Não Reconheço") == "Nao Reconheco"
    assert _strip_accents("câmbio é ótimo") == "cambio e otimo"
    assert _strip_accents("") == "" and _strip_accents(None) == ""


def test_vars_norm_no_contexto():
    ctx = _build_conditional_context(
        user_input="Não Reconheço a Compra", output="Resposta com AÇÃO")
    assert ctx["input_norm"] == "nao reconheco a compra"
    assert ctx["output_norm"] == "resposta com acao"
    assert "text_norm" in ctx
    # todas presentes sempre (mesmo sem input/output)
    empty = _build_conditional_context()
    for k in ("input_norm", "output_norm", "text_norm"):
        assert empty[k] == ""


def test_a_dor_real_uma_grafia_casa_ambas():
    """'nao reconheco' (sem acento) casa 'Não Reconheço' — o autor escreve UMA vez."""
    ctx = _build_conditional_context(user_input="quero avisar: Não Reconheço essa compra")
    assert _eval_conditional("'nao reconheco' in input_norm", ctx) is True
    # e o inverso: mesmo digitando com acento, o strip do termo (na UI) casa
    assert _eval_conditional("'nao reconheco' in input_norm", ctx) is True


def test_legado_lower_intacto():
    """Reprodutibilidade: exprs existentes sobre *_lower não mudam de resultado."""
    ctx = _build_conditional_context(user_input="Não Reconheço", output="PIX enviado")
    assert _eval_conditional("'não reconheço' in input_lower", ctx) is True
    assert _eval_conditional("'pix' in output_lower", ctx) is True
    # input_lower NÃO perde acento (continua como era)
    assert ctx["input_lower"] == "não reconheço"


def test_output_norm_no_fast_routing():
    """output_norm deriva do output → precisa estar em _OUTPUT_CLASS_VARS senão o
    fast-routing pularia o router numa regra que a usa (achado do mapa)."""
    assert "output_norm" in _OUTPUT_CLASS_VARS
    assert _expr_uses_output("'x' in output_norm") is True
    # input_norm/text_norm NÃO são classe-output (rota decidida sem o router)
    assert _expr_uses_output("'x' in input_norm") is False


def test_vars_norm_no_meta_para_ui():
    """As 3 vars aparecem no vocabulário (senão o NL→Jinja marca como unknown)."""
    names = {v["name"] for v in CONDITIONAL_VARS_META}
    assert {"input_norm", "output_norm", "text_norm"} <= names


def test_card_gera_com_norm_por_default():
    src = PG.read_text(encoding="utf-8")
    # estado + helper JS espelhando o _strip_accents do engine
    assert "kwNorm: true" in src
    assert "_stripAccents(s)" in src and "normalize('NFKD')" in src
    # mapeamento _lower → _norm no _draftExpr + checkbox
    assert "output_lower: 'output_norm'" in src
    assert 'data-testid="kw-norm"' in src
    assert "editor.kwNorm" in src
