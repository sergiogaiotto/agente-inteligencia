"""Trava anti-drift: padrao canonico de cores por *kind* de agente em toda a UI.

Padrao (pedido do usuario, 2026-06-06): AR/router = laranja, AOBD = vermelho,
SA = verde. Materializado na plataforma como:

    router               -> orange
    aobd / orchestrator  -> rose   (familia vermelha do palette permitido)
    subagente / else     -> teal   (familia verde do palette permitido)

Telas de referencia ja conformes antes desta trava: agents.html, settings.html,
workspace.html. Este teste FIXA o padrao e impede a regressao que deixava o
AI Mesh (mesh.html) e skills.html pintando roteadores de AZUL (brand) e o
orquestrador de RED puro -- divergencia silenciosa por ternarios hardcoded
duplicados em cada template.

Nota: esta trava NAO substitui test_palette_no_purple_tones (que bloqueia roxo);
ela e ortogonal e cuida especificamente do mapeamento kind -> cor.
"""
import re
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[1] / "app" / "templates" / "pages"

# Toda superficie que materializa kind -> cor (ternarios Alpine ou helpers JS).
KIND_COLOR_FILES = [
    "agents.html",
    "settings.html",
    "workspace.html",
    # mesh.html (página Topologia) aposentada no PR-B2/B3 — o Fluxograma
    # (mesh_flow.html) usa o KIND map central, fora do escopo deste guard.
    "skills.html",
]

# Familias de cor que router/orquestrador JAMAIS podem usar.
# - brand/blue/sky/indigo/cyan: azul (o bug do AI Mesh).
# - red puro: deve ser padronizado em "rose" para o orquestrador.
FORBIDDEN_FAMILY = r"(?:brand|blue|sky|indigo|cyan|red)"

# Chaves que representam o papel "orquestrador" (AOBD em agentes / orchestrator em skills).
ORCH_KEYS = ("aobd", "orchestrator")


def _read(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", KIND_COLOR_FILES)
def test_router_is_orange_never_blue(name):
    """router precisa ser orange e nunca azul/brand/red em nenhuma tela."""
    text = _read(name)
    bad = re.findall(rf"router'\s*\?\s*'(?:bg|text)-{FORBIDDEN_FAMILY}\b", text)
    assert not bad, f"{name}: router pintado com cor proibida {bad} (deve ser orange)"
    assert re.search(r"router'\s*\?\s*'(?:bg|text)-orange", text), (
        f"{name}: nenhum mapeamento router->orange encontrado"
    )


@pytest.mark.parametrize("name", KIND_COLOR_FILES)
def test_orchestrator_is_rose_never_blue_or_red(name):
    """aobd/orchestrator precisa ser rose e nunca azul nem red puro."""
    text = _read(name)
    found_rose = False
    for key in ORCH_KEYS:
        bad = re.findall(rf"{key}'\s*\?\s*'(?:bg|text)-{FORBIDDEN_FAMILY}\b", text)
        assert not bad, f"{name}: {key} pintado com cor proibida {bad} (deve ser rose)"
        if re.search(rf"{key}'\s*\?\s*'(?:bg|text)-rose", text):
            found_rose = True
    assert found_rose, f"{name}: nenhum mapeamento aobd/orchestrator->rose encontrado"


@pytest.mark.parametrize("name", KIND_COLOR_FILES)
def test_specialist_is_teal(name):
    """subagente/especialista (ramo else) usa teal."""
    text = _read(name)
    assert re.search(r"(?:bg|text)-teal", text), f"{name}: falta tom teal (subagente)"


def test_no_page_paints_router_or_orchestrator_with_forbidden_color():
    """Varre TODA pages/*.html: qualquer arquivo (inclusive novos) que pinte
    router/aobd/orchestrator de azul ou red puro falha aqui. E a defesa que
    pega drift em telas que ainda nem existem."""
    offenders = []
    for path in sorted(PAGES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for key in ("router",) + ORCH_KEYS:
            hits = re.findall(rf"{key}'\s*\?\s*'(?:bg|text)-{FORBIDDEN_FAMILY}\b", text)
            if hits:
                offenders.append((path.name, key, hits))
    assert not offenders, f"Cores proibidas para kind encontradas: {offenders}"


def test_skills_regression_no_blue_router_no_red_orchestrator():
    """skills.html tinha o mesmo drift (router azul, orchestrator red)."""
    text = _read("skills.html")
    assert "router'?'bg-brand" not in text
    assert "router'?'text-brand" not in text
    assert "orchestrator'?'bg-red" not in text
    assert "orchestrator'?'text-red" not in text
