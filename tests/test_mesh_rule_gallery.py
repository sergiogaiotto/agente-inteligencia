"""Galeria de Intenções (Fatia 2, 2026-06-18) — UX amigável para a regra
condicional do Fluxograma.

O operador se confundia escrevendo Jinja cru (`'pix' in output_lower`) num
textarea vazio, com as ~20 variáveis escondidas num endpoint órfão. A Galeria
inverte: cards de intenção em pt-BR (palavra-chave / anexo / decisão) montam o
Jinja pra ele, com a "Regra gerada" visível e escape hatch para edição manual.

Como o frontend é Alpine (JS), os testes garantem:
1. O vocabulário que a galeria referencia EXISTE de fato em CONDITIONAL_VARS_META
   (trava de drift — fonte única do engine, não strings soltas no template).
2. O template está cabeado (consome /conditional-vars, tem os 3 cards, compila
   para editor.expr, e preserva o escape hatch + buildConfig sem mudar runtime).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATE = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "mesh_flow.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


# ─── 1. Vocabulário da galeria ⊆ fonte única do engine (anti-drift) ──────────

# Variáveis que os cards da galeria geram. Se a galeria oferecer um var que o
# runtime não monta, a regra falharia em silêncio (ChainableUndefined → falsy).
GALLERY_VARS = {
    "output_lower", "input_lower", "text_all",      # card palavra-chave (Onde:)
    "has_attachments", "has_document", "has_image",  # card anexo
    "is_recommend", "is_refuse", "is_escalate", "final_state",  # card decisão
    "contains_url", "contains_pdf", "contains_image",  # card conteúdo (Fatia 3)
    "has_output", "output_length",                   # card tamanho (Fatia 3)
    "inputs",                                        # card parâmetro exato (Postura B)
}


def test_gallery_vars_exist_in_engine_meta():
    from app.agents.engine import _build_conditional_context, CONDITIONAL_VARS_META

    runtime_keys = set(_build_conditional_context().keys())
    meta_names = {v["name"] for v in CONDITIONAL_VARS_META}
    missing_runtime = GALLERY_VARS - runtime_keys
    missing_meta = GALLERY_VARS - meta_names
    assert not missing_runtime, f"galeria referencia vars ausentes no runtime: {missing_runtime}"
    assert not missing_meta, f"galeria referencia vars sem metadata (vars panel): {missing_meta}"


def test_conditional_vars_endpoint_serves_gallery_vocab():
    """O endpoint antes órfão agora alimenta a galeria — deve expor o vocabulário."""
    import asyncio
    from app.routes.mesh import conditional_vars

    data = asyncio.run(conditional_vars())
    names = {v["name"] for v in data["vars"]}
    assert GALLERY_VARS.issubset(names)


# ─── 2. Template cabeado ─────────────────────────────────────────────────────

def test_template_fetches_conditional_vars(html: str):
    """load() consome o endpoint antes órfão e guarda em condVars."""
    assert "/api/v1/mesh/conditional-vars" in html
    assert "this.condVars = cv.vars" in html
    assert "condVars: []" in html


def test_template_has_intent_cards(html: str):
    # núcleo (Fatia 2) + conteúdo/tamanho (Fatia 3) + parâmetro exato (Postura B)
    for intent in ("'keyword'", "'attachment'", "'decision'", "'content'", "'size'", "'param'"):
        assert f"selectIntent({intent})" in html, f"card de intenção {intent} ausente"


def test_param_card_generates_inputs_rule(html: str):
    """Card 'Parâmetro exato' (Postura B): gera `inputs.<campo> <op> <valor>` — roteia
    por valor de arg selado, sem IA. Valor de texto é EXATO (sem lowercase)."""
    assert "selectIntent('param')" in html
    assert 'data-testid="intent-param"' in html
    assert 'data-testid="param-field"' in html and 'data-testid="param-value"' in html
    # _draftExpr monta `inputs.${f} ${op} ${val}`
    assert "`inputs.${f} ${op} ${val}`" in html
    # valor de texto é EXATO (helper sem lowercase)
    assert "_jinjaStrExact(" in html


def test_param_card_guards_invalid_input(html: str):
    """Guardas (revisão adversarial): entrada inválida geraria Jinja quebrado que,
    no fail-open do gate, rodaria SEMPRE (oposto do intent). O card previne e avisa."""
    # campo precisa ser identificador válido (senão `inputs.cd cliente` estoura)
    assert "/^[A-Za-z_]\\w*$/.test(f)" in html
    # decimal pt-BR normalizado (1,5 → 1.5)
    assert "raw.replace(',', '.')" in html
    # maior/menor exige número (comparar número com texto estoura no runtime)
    assert "if (ordering && !isNum) return ''" in html
    # feedback inline (não falha em silêncio)
    assert 'data-testid="param-field-hint"' in html and 'data-testid="param-value-hint"' in html
    assert "_paramFieldOk()" in html and "_paramValueOrderingOk()" in html


def test_gallery_compiles_to_editor_expr(html: str):
    """syncExpr() escreve a regra montada em editor.expr — buildConfig usa expr,
    então NÃO há mudança no modelo de execução (mesma config.expr de sempre)."""
    assert "syncExpr()" in html
    assert "ed.expr = this._compose(" in html
    # buildConfig continua lendo editor.expr (runtime intacto)
    assert "cfg.expr = ed.expr.trim()" in html


def test_combine_clauses_with_and_or(html: str):
    """Fatia 3: múltiplas condições combinadas por E/OU parentetizado."""
    assert "addClause()" in html
    assert "removeClause(" in html
    assert "_needsParens(" in html         # protege precedência ao combinar
    assert 'x-model="editor.join"' in html
    assert 'value="and"' in html and 'value="or"' in html


def test_content_and_size_cards_use_real_vars(html: str):
    """Cards de conteúdo/tamanho geram vars que existem no engine."""
    for v in ("contains_url", "contains_pdf", "contains_image"):
        assert f'value="{v}"' in html
    assert "not has_output" in html        # tamanho: vazia
    assert "output_length < " in html and "output_length > " in html


def test_content_card_renamed_and_points_to_keyword(html: str):
    """O card 'content' foi renomeado p/ 'Tipo de mídia' (era 'Conteúdo', que o
    operador confundia com 'contém o texto X') e aponta o match textual p/ o card
    'Palavra-chave'. O intent interno 'content' permanece (sem mudar runtime)."""
    assert "<span>Tipo de mídia</span>" in html
    assert "<span>Conteúdo</span>" not in html
    assert "selectIntent('content')" in html          # intent/runtime inalterado
    # o corpo do card cross-referencia o card de texto
    assert "Palavra-chave" in html and "texto específico" in html


def test_did_you_mean_wired(html: str):
    """did-you-mean (Levenshtein) ataca o typo que falha em silêncio."""
    assert "exprWarnings()" in html
    assert "fixVar(w)" in html
    assert "_lev(" in html
    # buildConfig continua lendo editor.expr (runtime intacto)
    assert "cfg.expr = ed.expr.trim()" in html


def test_keyword_card_offers_the_three_scopes(html: str):
    for v in ("output_lower", "input_lower", "text_all"):
        assert f'value="{v}"' in html


def test_decision_card_maps_to_shortcuts(html: str):
    assert 'value="is_recommend"' in html
    assert 'value="is_refuse"' in html
    assert 'value="is_escalate"' in html
    assert "final_state == &#39;LogAndClose&#39;" in html or "final_state == 'LogAndClose'" in html


def test_escape_hatch_present_both_directions(html: str):
    """Galeria → manual e manual → galeria; power user nunca fica preso.
    (Copy para leigo: o link agora é 'escrever à mão (avançado)'.)"""
    assert "switchToManual()" in html
    assert "backToGallery()" in html
    assert "escrever à mão" in html


def test_manual_mode_shows_vars_panel(html: str):
    """No modo manual, o painel de variáveis (clicável) ataca a raiz do
    problema: o vocabulário some atrás de um endpoint não-consumido."""
    assert "insertVar(v.name)" in html
    assert 'x-for="v in condVars"' in html


def test_existing_rule_opens_in_manual_mode(html: str):
    """Regra já salva abre em manual (nunca reinterpreta/perde a expr)."""
    assert "(cfg.expr || '').trim() ? 'manual' : 'gallery'" in html


def test_regra_gerada_strip_is_readonly(html: str):
    """A regra montada é visível (read-only) no modo galeria.
    (Copy para leigo: rótulo agora é 'Regra montada'.)"""
    assert "Regra montada" in html
    assert 'x-text="editor.expr"' in html
