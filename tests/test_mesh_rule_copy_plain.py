"""Copy para leigo no editor de conexão (2026-06-19).

Usuário pediu: nada de termos técnicos (Jinja, scoped, output, mesh_connections,
FSM, MIME, lowercase…) na UI — linguagem simples. Estes testes travam a
propriedade "sem jargão" no que o usuário VÊ (modal + descrições dos campos),
sem tocar nos value=/identificadores por trás (que continuam em inglês).
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "app" / "templates" / "pages" / "mesh_flow.html"


@pytest.fixture(scope="module")
def html() -> str:
    return _TEMPLATE.read_text(encoding="utf-8")


# ─── Modal: rótulos visíveis sem jargão, mas value= preservados ──────────────

def test_scope_options_have_no_jargon_but_keep_values(html: str):
    # texto visível antigo (técnico) saiu
    assert "transforma via Jinja" not in html
    assert "output completo vira contexto" not in html
    assert "só a solicitação original" not in html
    # texto novo, para leigo, entrou
    assert "Tudo (padrão) — passa a resposta inteira do agente anterior" in html
    assert "Resumir (avançado) — corta ou encurta a resposta" in html
    assert "Começar do zero — só a pergunta original" in html
    # os value= (chaves de runtime) NÃO podem mudar
    assert 'value="inherit"' in html
    assert 'value="scoped"' in html
    assert 'value="isolated"' in html


def test_manual_label_drops_jinja_word(html: str):
    assert "expressão Jinja booleana" not in html
    assert "Regra (modo avançado)" in html


def test_footer_has_no_table_name(html: str):
    assert "mesh_connections</span>" not in html
    assert "a fonte do grafo executável" not in html
    assert "Esta conexão é salva e passa a valer no fluxo." in html


def test_simulation_labels_are_plain(html: str):
    assert "Contexto de teste" not in html
    assert "Simular com estes dados" in html
    # placeholders sem (output)/(input)
    assert "resposta (output)" not in html
    assert "pergunta (input)" not in html


def test_connection_type_help_is_plain(html: str):
    # CONN_TYPES desc no JS do modal — sem output/input/roteamento 1-de-N
    assert "recebe o output dela" not in html
    assert "Roteamento 1-de-N" not in html
    assert "agente seguinte" in html  # vocabulário leigo adotado


# ─── Campos disponíveis (CONDITIONAL_VARS_META.desc) sem jargão ──────────────

JARGON_TOKENS = ["lowercase", "case-insensitive", "MIME", "FSM", "upstream", "True se", "Atalho para"]


def test_field_descriptions_have_no_jargon():
    from app.agents.engine import CONDITIONAL_VARS_META
    offenders = []
    for v in CONDITIONAL_VARS_META:
        desc = v["desc"]
        for tok in JARGON_TOKENS:
            if tok in desc:
                offenders.append((v["name"], tok))
    assert not offenders, f"descrições com jargão: {offenders}"


def test_field_names_and_types_preserved():
    """A copy mudou só o 'desc' — name/type (IDs de runtime) permanecem."""
    from app.agents.engine import CONDITIONAL_VARS_META
    names = {v["name"] for v in CONDITIONAL_VARS_META}
    # amostra de IDs que a galeria/tradutor dependem
    for n in ("output_lower", "input_lower", "has_document", "is_refuse", "final_state", "contains_url"):
        assert n in names
    for v in CONDITIONAL_VARS_META:
        assert v["type"] in {"str", "int", "bool", "float"}
        assert len(v["desc"]) > 10
