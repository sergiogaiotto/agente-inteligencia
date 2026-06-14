"""Guard: o filtro de Tipos do Catálogo cobre TODOS os kinds válidos.

Bug que motivou (2026-06-13): o kind `pipeline` (1ª classe desde o Estúdio de
Pipelines, #364) nunca foi adicionado ao dropdown de Tipos em catalog.html —
então não dava pra filtrar pipelines, justamente o tipo de 100% das entries da
tela. Este guard impede que um novo kind seja esquecido no filtro de novo.

Fonte da verdade dos kinds: app/catalog/urn.VALID_KINDS.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.catalog.urn import VALID_KINDS

CATALOG_HTML = Path(__file__).resolve().parents[1] / "app" / "templates" / "pages" / "catalog.html"


def _filter_kind_options() -> set[str]:
    """Extrai os value= das <option> do <select x-model="filterKind">."""
    html = CATALOG_HTML.read_text(encoding="utf-8")
    # bloco do select de filterKind até o </select>
    m = re.search(r'x-model="filterKind".*?</select>', html, re.S)
    assert m, "select filterKind não encontrado em catalog.html"
    return set(re.findall(r'<option value="([^"]*)"', m.group(0)))


def test_filter_covers_all_valid_kinds():
    """Todo kind de urn.VALID_KINDS precisa ter uma opção no filtro de Tipos."""
    options = _filter_kind_options()
    missing = set(VALID_KINDS) - options
    assert not missing, f"kinds ausentes no filtro de Tipos do Catálogo: {sorted(missing)}"


def test_pipeline_is_filterable():
    """Regressão direta do bug: 'pipeline' tem que estar no filtro."""
    assert "pipeline" in _filter_kind_options()


def test_filter_has_empty_all_option():
    """Mantém a opção 'Todos tipos' (value vazio)."""
    assert "" in _filter_kind_options()
