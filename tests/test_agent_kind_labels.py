"""Padronização dos nomes de camada de agente (2026-06-19).

Usuário pediu: usar SEMPRE Maestro (aobd) / Triagem (router/sr) / Especialista
(subagent/sa) na UI — nada de Subagent/Router/Orchestrator (inglês) nem
Orquestrador/Roteador/Subagente, e SEM o código (AOBD/AR/SA) no rótulo.

Fonte única: helper global `window.agentKindLabel(kind)` em base.html. Estes
testes travam (1) o helper e seu mapa, (2) os dropdowns/badges não exibirem
mais os termos antigos, e (3) os value=/chaves continuarem em inglês.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def base_html() -> str:
    return _read("app/templates/layouts/base.html")


# ─── Fonte única: helper global ──────────────────────────────────────────────

def test_helper_defined_in_base(base_html: str):
    assert "window.agentKindLabel" in base_html
    # mapa canônico presente
    for frag in ("aobd: 'Maestro'", "orchestrator: 'Maestro'", "router: 'Triagem'", "subagent: 'Especialista'"):
        assert frag in base_html, f"mapa do helper faltando: {frag}"


# ─── Páginas usam o helper, não os termos antigos ────────────────────────────

PAGES_USING_HELPER = [
    "app/templates/pages/skills.html",
    "app/templates/pages/settings.html",
    "app/templates/pages/agents.html",
    "app/templates/pages/agent_form.html",
    "app/templates/pages/workspace.html",
    "app/templates/pages/skill_form.html",
    "app/templates/pages/catalog_publish.html",
    "app/templates/pages/catalog_detail.html",
]


@pytest.mark.parametrize("page", PAGES_USING_HELPER)
def test_page_references_helper(page: str):
    assert "agentKindLabel" in _read(page), f"{page} não usa o helper canônico"


# ─── Dropdowns de tipo: rótulo canônico, value= preservado ───────────────────

@pytest.mark.parametrize("page", [
    "app/templates/pages/skills.html",
    "app/templates/pages/agents.html",
    "app/templates/pages/settings.html",
    "app/templates/pages/skill_form.html",
])
def test_dropdowns_use_canonical_labels(page: str):
    html = _read(page)
    # rótulos antigos em inglês não aparecem mais como texto de <option>
    for old in (">Subagent<", ">Router<", ">Orchestrator<", ">Subagente<", ">Roteador<", ">Orquestrador<"):
        assert old not in html, f"{page} ainda tem option label antigo: {old}"
    # os value= (chaves de API/JS) permanecem em inglês
    assert 'value="router"' in html
    assert ('value="subagent"' in html) or ('value="orchestrator"' in html) or ('value="aobd"' in html)


def test_no_code_suffix_labels_remain():
    """Formato 'só o nome': nada de 'Maestro (AOBD)' / 'Triagem (AR)' / 'Especialista (SA)'."""
    for page in ("app/templates/pages/dashboard.html", "app/templates/pages/observability.html"):
        html = _read(page)
        for bad in ("Maestro (AOBD)", "Triagem (AR)", "Especialista (SA)"):
            assert bad not in html, f"{page} ainda mostra código no rótulo: {bad}"


def test_canonical_names_present_in_dashboard():
    html = _read("app/templates/pages/dashboard.html")
    for name in (">Maestro<", ">Triagem<", ">Especialista<"):
        assert name in html
