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
    # [review 9] fórmula EXATA do contador de runs — 'carregados' solto já
    # existia 3x no arquivo e o assert antigo passava sem a feature.
    assert "' de '+evalRuns.length+' carregados'" in src, (
        "contador das execuções perdeu a fórmula 'X de Y carregados'"
    )


def test_gold_counter_discloses_window_overflow():
    """[review 1/6] gold carrega com limit explícito e o contador expõe o
    recorte quando o dataset excede a janela ('N no total')."""
    src = _src()
    assert "/api/v1/gold-cases?limit=1000" in src, (
        "load() voltou ao default de 50 casos — contador/dropdowns mentem"
    )
    assert "goldTotal>goldCases.length" in src, (
        "contador do gold não expõe o overflow da janela"
    )
    assert "no total" in src


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
    for getter in ("goldVersions", "goldCategories"):
        m = re.search(r"get " + getter + r"\(\)\{.*?\},", js, re.S)
        assert m, f"getter {getter} ausente"  # [review 12] assert antes do group
        assert "new Set" in m.group(0) and ".sort()" in m.group(0), (
            f"{getter} não deriva/ordena dos dados"
        )


def test_clear_replaces_object_reference():
    """clearXFilter atribui um objeto NOVO (não muta campo a campo) — o
    proxy do Alpine reage à troca de referência de forma atômica."""
    js = _page_js()
    assert re.search(r"clearGoldFilter\(\)\{this\.goldFilter=\{", js)
    assert re.search(r"clearRunFilter\(\)\{this\.runFilter=\{", js)


def test_search_input_debounced():
    """Busca com debounce — sem isso cada tecla re-filtra a lista inteira
    em datasets grandes. [review 13] vinculado ao campo goldFilter.q (o
    assert solto viraria tautológico no 1º outro input com debounce)."""
    assert re.search(r'x-model\.debounce[.\w]*="goldFilter\.q"', _src()), (
        "busca do gold sem debounce vinculado a goldFilter.q"
    )


def test_target_dropdown_offers_pipelines_too():
    """[review 10] o alvo filtra agente OU pipeline — o dropdown precisa
    oferecer os DOIS (com o mesmo glifo 🔗 do resto da página)."""
    src = _src()
    m = re.search(r'data-testid="runs-filter-bar".*?</div>\s*<div', src, re.S)
    assert m, "barra de filtros das execuções ausente"
    blk = m.group(0)
    assert 'x-for="p in pipelines"' in blk, "dropdown de alvo sem pipelines"
    assert "🔗" in blk, "glifo de pipeline divergente do resto da página"
    assert "⛓" not in blk


def test_expanded_run_pinned_in_filtered_list():
    """[review 2] o run expandido fica PINADO na lista filtrada — o poll
    re-hidrata e um queued→completed sumiria do filtro de status no meio
    do acompanhamento, colapsando o detalhe aberto."""
    js = _page_js()
    m = re.search(r"get filteredEvalRuns\(\)\{.*?\n    \},", js, re.S)
    assert m, "getter filteredEvalRuns ausente"
    assert "r.id===this.expandedEval" in m.group(0), (
        "run expandido não está pinado — some da lista no meio do poll"
    )


def test_stale_filter_values_pruned_on_load():
    """[review 3/7] opção que sumiu dos dados deixaria o select vazio
    (parecendo neutro) com o model ainda filtrando tudo — load() poda."""
    js = _page_js()
    assert "_pruneStaleFilters" in js, "poda de filtro fantasma ausente"
    m = re.search(r"_pruneStaleFilters\(\)\{.*?\n    \},", js, re.S)
    assert m, "corpo do _pruneStaleFilters ausente"
    blk = m.group(0)
    for campo in ("goldFilter.version", "goldFilter.category",
                  "runFilter.target", "runFilter.release"):
        assert campo in blk, f"poda não cobre {campo}"


def test_status_dropdown_covers_persisted_terminals():
    """[review 4] budget_exceeded e os skips são status REAIS de eval_runs
    — sem eles no dropdown, runs abortados ficam inencontráveis."""
    src = _src()
    m = re.search(r'x-model="runFilter\.status".*?</select>', src, re.S)
    assert m, "select de status ausente"
    blk = m.group(0)
    for st in ("budget_exceeded", "no_cases", "invalid_agent",
               "invalid_pipeline", "invalid_target"):
        assert st in blk, f"status persistido '{st}' fora do dropdown"


def test_actions_clear_conflicting_filters():
    """[review 5] salvar um caso (ou disparar um run) que o filtro ativo
    esconderia limpa o filtro — você sempre vê o que acabou de criar."""
    js = _page_js()
    assert "if (this.goldFilterActive) this.clearGoldFilter();" in js, (
        "createGoldCase não limpa o filtro — caso salvo 'some' com sucesso"
    )
    assert "if(this.runFilterActive)this.clearRunFilter();" in js, (
        "executeHarness 202 não limpa o filtro — run disparado invisível"
    )
