"""Blindagem da atualização + navegabilidade do Guia Interativo (Tour).

Trava três frentes desta rodada:

A) CONTEÚDO ATUALIZADO — os passos passam a refletir a plataforma 40.x–42.x
   (Menu de Regência no botão direito, Simulador de roteamento, "Converse com
   seu agente", cobertura per-tool + chip legado, atribuição de usuário
   dono/ator, "IA: descrever" no Playground, Golden Dataset editável).
B) SUBMENU COM MENU FECHADO — passos ancorados em item de submenu carregam
   `submenu:'catalog'|'mesh'`, e o tour abre a sidebar + o submenu dono antes
   de posicionar (senão o item fica display:none em 0,0).
C) CLAMP NA VIEWPORT — o card do tour é preso dentro da tela (Math.min/max nos
   dois eixos, usando o tamanho REAL do card via x-ref), então a explicação
   nunca some pela borda, seja qual for o tamanho do browser.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "app" / "templates" / "layouts" / "base.html"


def _src() -> str:
    return BASE.read_text(encoding="utf-8")


# ── A. Conteúdo atualizado para 40.x–42.x ────────────────────────────
def test_tour_mesh_step_mentions_new_canvas_superpowers():
    src = _src()
    for marker in ("Menu de Regência", "Simulador de roteamento", "Converse com seu agente"):
        assert marker in src, f"passo do AI Mesh não menciona: {marker}"


def test_tour_tools_step_mentions_per_tool_coverage_and_legacy():
    src = _src()
    assert "per-tool" in src, "passo de Ferramentas não menciona modo per-tool"
    assert "legado" in src, "passo de Ferramentas não menciona o chip legado"


def test_tour_mentions_user_attribution():
    """A atribuição de usuário (dono/ator) precisa aparecer nas telas de análise."""
    src = _src()
    assert "DONO" in src or "dono" in src, "tour não menciona o dono da interação"
    assert "ator" in src.lower(), "tour não menciona o ator na auditoria"
    assert "via chave" in src, "tour não menciona o badge 'via chave'"


def test_tour_playground_step_mentions_ia_describe_and_attachments():
    src = _src()
    assert "IA: descrever" in src, "Playground não menciona o tradutor 'IA: descrever'"
    assert "attachments" in src, "Playground não menciona o bloco de anexos no codegen"


def test_tour_harness_step_mentions_editable_gold_dataset():
    src = _src()
    # trecho do passo de Avaliação
    assert "Golden Dataset é editável" in src, "Harness não menciona Golden Dataset editável"


# ── B. Submenu ancorado + abertura no menu fechado ───────────────────
def test_catalog_submenu_steps_carry_submenu_field():
    src = _src()
    # os quatro itens do submenu de Catálogo precisam do campo submenu:'catalog'
    for el in ("tour-nav-catalog-queue", "tour-nav-catalog-inventory",
               "tour-nav-catalog-stewardship", "tour-nav-catalog-cost"):
        # localiza o objeto do passo e confirma que traz submenu:'catalog'
        m = re.search(r"\{el:'" + re.escape(el) + r"'.*?\}", src, re.S)
        assert m, f"passo ausente para {el}"
        assert "submenu:'catalog'" in m.group(0), f"{el} sem submenu:'catalog'"


def test_mesh_submenu_steps_carry_submenu_field():
    src = _src()
    for el in ("tour-nav-workspace", "tour-nav-playground"):
        m = re.search(r"\{el:'" + re.escape(el) + r"'.*?\}", src, re.S)
        assert m, f"passo ausente para {el}"
        assert "submenu:'mesh'" in m.group(0), f"{el} sem submenu:'mesh'"


def test_ensure_visible_opens_sidebar_and_submenu():
    src = _src()
    assert "_ensureTourTargetVisible()" in src, "método _ensureTourTargetVisible ausente"
    # abre a sidebar e o submenu dono do passo
    assert "if (this.sidebarCollapsed) this.sidebarCollapsed = false;" in src
    assert "if (sm === 'catalog') this.catalogSubmenuOpen = true;" in src
    assert "if (sm === 'mesh') this.meshSubmenuOpen = true;" in src


def test_ensure_visible_opens_mobile_drawer_too():
    """Em < lg a sidebar é off-canvas por sidebarOpen (-translate-x-full): sem abrir,
    23/25 passos apontam para fora da tela (tela escura sem destaque)."""
    src = _src()
    assert "if (!this.sidebarOpen) this.sidebarOpen = true;" in src, \
        "_ensureTourTargetVisible não abre o drawer mobile (sidebarOpen)"


def test_tour_restores_menu_state_on_end():
    """Ao encerrar, o tour devolve sidebar/submenus como o usuário deixou."""
    src = _src()
    assert "_tourSaved" in src, "tour não guarda o estado do menu"
    assert "this.sidebarCollapsed = s.collapsed;" in src
    assert "this.sidebarOpen = s.open;" in src
    assert "this.catalogSubmenuOpen = s.catalog;" in src
    assert "this.meshSubmenuOpen = s.mesh;" in src
    assert "open: this.sidebarOpen," in src, "_tourSaved não guarda sidebarOpen"


# ── C. Clamp do card na viewport ─────────────────────────────────────
def test_position_tour_clamps_both_axes():
    src = _src()
    assert "positionTour()" in src, "método positionTour ausente"
    # clamp horizontal e vertical dentro da viewport
    assert "vw - cw - M" in src, "sem clamp horizontal (largura da viewport)"
    assert "vh - ch - M" in src, "sem clamp vertical (altura da viewport) — bug do card cortado"
    # usa o tamanho REAL do card medido via x-ref
    assert 'this.$refs.tourCard' in src, "positionTour não mede o card real (x-ref)"


def test_card_uses_reactive_state_not_unbounded_getter():
    src = _src()
    # bindings novos, reativos
    assert 'x-ref="tourCard"' in src, "card do tour sem x-ref para medir a altura"
    assert ':style="tourCardStyle"' in src, "card não usa o estilo reativo tourCardStyle"
    assert ':style="tourSpotStyle"' in src, "spotlight não usa o estilo reativo tourSpotStyle"
    # os getters antigos, sem clamp vertical, foram removidos
    assert "get cardPosition()" not in src, "getter cardPosition legado (sem clamp) ainda presente"
    assert "get spotlightStyle()" not in src, "getter spotlightStyle legado ainda presente"


def test_tour_repositions_on_resize():
    """Redimensionar o browser reposiciona o card (era a causa do 'some na tela').
    Passa por _repositionTour: cruzar o breakpoint lg dispara a transição da sidebar
    e precisa da re-medição atrasada (não só do positionTour imediato)."""
    src = _src()
    assert '@resize.window="_repositionTour()"' in src, "tour não reposiciona no resize"


def test_tour_remeasures_after_sidebar_transition():
    """A sidebar tem transition-all duration-200: medir 1 frame após abrir pega o rect
    NO MEIO da animação e congela spotlight/card errados. Precisa re-medir após assentar."""
    src = _src()
    assert "_tourSettleTimer" in src, "sem re-medição pós-transição (timer de assentamento)"
    assert "setTimeout(() => { if (this.tourActive) this.positionTour(); }, 250)" in src, \
        "re-medição não espera os 200ms da transição da sidebar"
    assert "clearTimeout(this._tourSettleTimer)" in src, "timer de assentamento nunca é limpo"


def test_tour_scroll_listener_uses_capture():
    """Scroll de container interno (nav overflow-y-auto) NÃO borbulha até window —
    só a fase capture enxerga e reposiciona o spotlight."""
    src = _src()
    assert '@scroll.window.capture="positionTour()"' in src, \
        "listener de scroll sem .capture — scroll interno da nav desalinha o spotlight"


def test_settings_step_parameter_count_claim_matches_code():
    """Claim numérica do passo de Configurações precisa bater com o produto
    (PARAMETER_UI_KEYS) — mesma classe da regressão 'rollback automático'."""
    import re
    from app.core.config import PARAMETER_UI_KEYS
    src = _src()
    n = len(PARAMETER_UI_KEYS)
    # A claim "~NN parâmetros" do passo de Configurações precisa ficar dentro
    # de ±5 do produto real (PARAMETER_UI_KEYS) — o número exato varia a cada
    # arco (43.0.0 harness +5, 49.0.0 loop do otimizador +6…).
    m = re.search(r"~(\d+) parâmetros", src)
    assert m, ("passo de Configurações perdeu a claim de contagem — "
               "re-ancore no PARAMETER_UI_KEYS")
    claimed = int(m.group(1))
    assert abs(claimed - n) <= 5, \
        f"claim '~{claimed}' divergiu do produto ({n} parâmetros) — atualize o passo"
