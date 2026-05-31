"""PR #238 — `_DEFAULT_MIN_RELEVANCE` mudou de 0.3 → 0.0.

# Motivação

Operador pediu: o threshold padrão de evidência (`min_relevance`) deveria
ser 0.0 (filtro mínimo desligado), a menos que a skill declare valor
maior. Antes era 0.3 — engine rejeitava silenciosamente evidências com
score < 0.3 quando a skill não dizia nada.

Mudança a pedido explícito (UI tela "Novo Agente > Revisão" e tela "Skill"
mostravam "0.30 (default do engine)"; agora mostram "0.00 (default do
engine)").

# Mudanças cobertas

1. `app/agents/engine.py:1442` — `_DEFAULT_MIN_RELEVANCE = 0.0`
2. `app/agents/engine.py:1682` — fallback do diagnóstico (0.3 → 0.0)
3. `app/templates/pages/skill_form.html` — 3 lugares: tooltip, placeholder,
   texto "vazio = default 0.XX"
4. `app/templates/pages/agent_form.html` — "0.XX (default do engine)"
5. `app/routes/wizard.py` — comentário que cita o default

Skills que declaram `min_relevance` explicitamente continuam aplicando o
valor declarado — não há mudança nesse caminho.
"""
from __future__ import annotations

import inspect
from pathlib import Path


def test_engine_default_min_relevance_is_zero():
    """A constante interna do engine fala por si — vira 0.0."""
    src = Path("app/agents/engine.py").read_text(encoding="utf-8")
    # Linha exata da constante (regex tolerante a whitespace)
    import re
    m = re.search(r"_DEFAULT_MIN_RELEVANCE\s*=\s*([0-9.]+)", src)
    assert m, "Constante _DEFAULT_MIN_RELEVANCE removida — atualizar este teste."
    value = float(m.group(1))
    assert value == 0.0, (
        f"_DEFAULT_MIN_RELEVANCE = {value}, esperava 0.0 (PR #238). "
        "Se foi mudança intencional, atualizar UI e este teste juntos."
    )


def test_engine_diagnostic_fallback_aligns_with_new_default():
    """O fallback do diagnóstico (`ctx.metadata.get(...) or X`) precisa bater
    com `_DEFAULT_MIN_RELEVANCE`. Se desalinhar, o operador vê threshold
    estranho no log quando a metadata está vazia."""
    src = Path("app/agents/engine.py").read_text(encoding="utf-8")
    # Bloco do diagnóstico: linha com `evidence_min_relevance"`)
    idx = src.find('ctx.metadata.get("evidence_min_relevance")')
    assert idx >= 0, "trecho de diagnóstico mudou — atualizar este teste"
    snippet = src[idx:idx + 200]
    import re
    m = re.search(r'or\s+([0-9.]+)', snippet)
    assert m, f"não achei o fallback `or X` no snippet: {snippet[:100]}"
    fallback = float(m.group(1))
    assert fallback == 0.0, (
        f"Fallback do diagnóstico vale {fallback} mas a constante é 0.0. "
        "Sem alinhamento, log mostra threshold diferente do que o engine usa."
    )


def test_skill_form_shows_zero_as_default_label():
    """Tela de Skill (skill_form.html) cita o default em 3 lugares: tooltip,
    placeholder do input, texto 'vazio = default X do engine'. PR #238
    atualizou para 0.00."""
    html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
    # Tooltip explicativo
    assert "Default do engine: 0.00" in html, (
        "Tooltip do label perdeu '0.00 (default do engine)'."
    )
    # Placeholder do input
    assert 'placeholder="0.00"' in html, (
        "Input placeholder não mostra '0.00' — operador vai ver 0.30 antigo."
    )
    # Texto auxiliar abaixo
    assert "vazio = default 0.00 do engine" in html, (
        "Texto 'vazio = default 0.00 do engine' regrediu para 0.30."
    )


def test_skill_form_does_not_still_show_old_0_30_in_user_facing_text():
    """Nenhum dos textos visíveis ao operador pode ainda dizer '0.30 do
    engine' (regressão silenciosa)."""
    html = Path("app/templates/pages/skill_form.html").read_text(encoding="utf-8")
    assert "default 0.30 do engine" not in html, (
        "Texto antigo '0.30 do engine' ainda presente. PR #238 mudou para 0.00."
    )
    assert "0.30 (default" not in html


def test_agent_form_review_shows_zero_as_default():
    """Tela de Agente (agent_form.html) — passo Revisão mostra
    `0.XX (default do engine)` quando a skill não declara min_relevance."""
    html = Path("app/templates/pages/agent_form.html").read_text(encoding="utf-8")
    assert "0.00 (default do engine)" in html, (
        "Tela Revisão do agente perdeu '0.00 (default do engine)'."
    )
    assert "0.30 (default do engine)" not in html, (
        "Tela Revisão ainda mostra '0.30 (default do engine)' — regrediu."
    )


def test_wizard_comment_mentions_new_default():
    """wizard.py emite YAML do ## Evidence Policy. Comentário cita o engine
    default. Não impacta runtime mas ajuda quem vai manter o código."""
    src = Path("app/routes/wizard.py").read_text(encoding="utf-8")
    # Comentário deve mencionar default 0.0 (não 0.3) próximo à lógica
    idx = src.find("_DEFAULT_MIN_RELEVANCE")
    assert idx >= 0, "comentário de referência cruzada para a constante sumiu"
    snippet = src[max(0, idx - 200):idx]
    assert "default 0.0" in snippet, (
        "Comentário no wizard ainda cita 0.3 — fonte de confusão para devs."
    )
