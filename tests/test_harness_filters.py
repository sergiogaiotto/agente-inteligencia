"""Filtros dos painéis Golden Dataset × Execuções (item 4, 51.0.0).

Client-side sobre o que está carregado: gold por versão/tipo/split/categoria/
busca; execuções por tipo/status/alvo/release. Contadores honestos ("X de Y",
e nas execuções "de Y carregados" — o recorte da janela fica explícito).
Blindagem por marcadores de template, mesma convenção dos testes do modal.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "app" / "templates" / "pages" / "harness.html"


def _src() -> str:
    return HARNESS.read_text(encoding="utf-8")


def _page_js(src: str | None = None) -> str:
    src = src or _src()
    m = re.search(r"function harnessPage\(\)\{.*?\n\}\}", src, re.S)
    assert m, "harnessPage() ausente"
    return m.group(0)


# ── Barras de filtro presentes e ligadas às listas ────────────────────
def test_filter_bars_present():
    src = _src()
    assert 'data-testid="gold-filter-bar"' in src
    assert 'data-testid="runs-filter-bar"' in src


def test_lists_iterate_filtered_collections():
    """As DUAS listas iteram as coleções FILTRADAS; os dropdowns do
    Comparar Execuções seguem na coleção crua (comparar não é filtrar)."""
    src = _src()
    assert 'x-for="c in filteredGoldCases"' in src, "lista do gold sem filtro"
    assert 'x-for="r in filteredEvalRuns"' in src, "lista de runs sem filtro"
    # os selects do Comparar continuam vendo TODOS os runs
    assert src.count('x-for="r in evalRuns"') >= 2, (
        "dropdowns do Comparar Execuções não devem herdar o filtro da lista"
    )


def test_honest_counters():
    src = _src()
    assert 'data-testid="gold-filter-count"' in src
    assert 'data-testid="runs-filter-count"' in src
    assert "' de '+goldCases.length" in src, "contador do gold sem o total"
    assert "carregados" in src, (
        "contador das execuções precisa dizer que filtra a JANELA carregada "
        "(convenção métricas sem falsa confiança)"
    )


def test_filtered_empty_states_offer_clear():
    """Filtro sem resultado ≠ painel vazio: cada estado tem mensagem
    própria e o filtrado oferece 'limpar'."""
    src = _src()
    assert "Nenhum caso no filtro atual" in src
    assert "Nenhum run no filtro atual" in src
    assert src.count("clearGoldFilter()") >= 2, "limpar do gold ausente"
    assert src.count("clearRunFilter()") >= 2, "limpar das execuções ausente"


# ── Semântica dos getters ─────────────────────────────────────────────
def test_gold_filter_covers_all_dimensions():
    js = _page_js()
    m = re.search(r"get filteredGoldCases\(\)\{.*?\n    \},", js, re.S)
    assert m, "getter filteredGoldCases ausente"
    blk = m.group(0)
    for marker in ("f.version", "f.case_type", "f.split", "f.category"):
        assert marker in blk, f"filtro do gold ignora {marker}"
    assert "input_text" in blk and "expected_output" in blk, (
        "busca q precisa varrer input E output"
    )
    assert "f.split==='sem'" in blk, (
        "opção 'sem split' (casos ainda não divididos) sumiu"
    )


def test_run_filter_covers_all_dimensions_and_both_targets():
    js = _page_js()
    m = re.search(r"get filteredEvalRuns\(\)\{.*?\n    \},", js, re.S)
    assert m, "getter filteredEvalRuns ausente"
    blk = m.group(0)
    for marker in ("f.run_type", "f.status", "f.release"):
        assert marker in blk, f"filtro de runs ignora {marker}"
    assert "r.agent_id===f.target" in blk and "r.pipeline_id===f.target" in blk, (
        "alvo precisa casar agente OU pipeline (runs têm um dos dois)"
    )


def test_version_and_category_options_derived_from_data():
    js = _page_js()
    assert "get goldVersions()" in js and "get goldCategories()" in js
    m = re.search(r"get goldVersions\(\)\{.*?\},", js, re.S)
    assert "new Set" in m.group(0) and ".sort()" in m.group(0)


def test_clear_replaces_object_reference():
    """clearXFilter atribui um objeto NOVO (não muta campo a campo) — o
    proxy do Alpine reage à troca de referência de forma atômica."""
    js = _page_js()
    assert re.search(r"clearGoldFilter\(\)\{this\.goldFilter=\{", js)
    assert re.search(r"clearRunFilter\(\)\{this\.runFilter=\{", js)


def test_search_input_debounced():
    """Busca com debounce — sem isso cada tecla re-filtra a lista inteira
    em datasets grandes."""
    assert "x-model.debounce" in _src(), "busca do gold sem debounce"
