"""UX: pontes de comunicação FONTE × REGRA (clareza evidência × RAG).

Contexto: usuários confundiam "Exigir evidências" (REGRA de recusa, global, em
Configurações) com RAG/Tabelas (FONTE de conhecimento, por agente, em /rag).
Estes testes garantem que os elos explicativos não regridam e que o rótulo
ambíguo "Exigir Evidência (RAG)" no agent_form — que na verdade controla a
CONSULTA ao RAG, não a recusa — foi renomeado.
"""
from __future__ import annotations

from pathlib import Path

import app.routes.frontend as fe

_ROOT = Path(fe.__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def test_settings_grounding_card_bridges_to_source():
    """Card 'Exigir evidências' aponta a FONTE (link /rag) e separa regra de fonte."""
    html = _read("app/templates/pages/settings.html")
    assert "Fonte das evidências" in html
    assert 'href="/rag"' in html


def test_agent_form_relabels_consulta_and_groups():
    """O toggle ambíguo virou 'Consultar bases…' e ganhou agrupamento + nota."""
    html = _read("app/templates/pages/agent_form.html")
    assert "Consultar bases de conhecimento" in html
    assert "Exigir Evidência (RAG)" not in html          # rótulo ambíguo removido
    assert "Conhecimento do agente" in html               # agrupamento
    assert 'href="/settings"' in html                     # nota da relação c/ regra global


def test_rag_page_bridges_back_to_rule():
    """A tela /rag explica que é a FONTE que alimenta a regra global."""
    html = _read("app/templates/pages/evidence.html")
    assert "alimenta a regra" in html
    assert 'href="/settings"' in html


def test_help_evidence_summary_explains_source_vs_rule():
    js = _read("app/static/js/help-content.js")
    assert "a FONTE" in js  # summary da key 'evidence' agora distingue fonte × regra


def test_edited_templates_are_valid_jinja():
    """Smoke: os templates tocados continuam sintaticamente válidos (sem quebrar o render)."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(_ROOT / "app" / "templates")))
    for t in ["pages/settings.html", "pages/agent_form.html",
              "pages/evidence.html", "layouts/base.html"]:
        src = env.loader.get_source(env, t)[0]
        env.parse(src)  # TemplateSyntaxError se inválido
