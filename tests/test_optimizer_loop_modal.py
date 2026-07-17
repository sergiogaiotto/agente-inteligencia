"""Modal de acompanhamento da Otimização automática (item 1, 50.0.0).

Ao clicar em "Otimizar automaticamente" o usuário passa a acompanhar o loop
num MODAL na própria tela de Avaliação: trilha na fila → champion → rodadas
(candidatos com score/pareto/reflexão + prompt sob demanda) → holdout →
veredito. Fechável (o job segue no servidor) e reabrível pelo card
("Acompanhar"). Blindagem por marcadores de template — mesma convenção dos
testes do tour e do harness.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "app" / "templates" / "pages" / "harness.html"


def _src() -> str:
    return HARNESS.read_text(encoding="utf-8")


# ── Estrutura do modal ────────────────────────────────────────────────
def test_modal_markup_present_with_dialog_role():
    src = _src()
    assert 'data-testid="optimizer-loop-modal"' in src, "modal ausente"
    m = re.search(
        r'data-testid="optimizer-loop-modal".*?aria-label="Acompanhamento da otimização automática"',
        src, re.S,
    )
    assert m, "modal sem role/aria-label de diálogo"
    assert 'role="dialog"' in m.group(0) or 'role="dialog"' in src


def test_modal_inside_optimizer_loop_scope():
    """O modal PRECISA ser filho do x-data optimizerLoop — fora dele,
    loopRun/loopCandidates não existem e o Alpine quebra em silêncio."""
    src = _src()
    card = src.find('data-testid="optimizer-loop-card"')
    modal = src.find('data-testid="optimizer-loop-modal"')
    drawer = src.find('aria-label="Detalhes do caso do Golden Dataset"')
    assert card != -1 and modal != -1
    assert card < modal < drawer, (
        "modal fora do card do optimizerLoop (ou depois do drawer do caso) — "
        "precisa estar DENTRO do escopo x-data"
    )


def test_modal_closes_by_escape_overlay_and_button():
    src = _src()
    i = src.find('data-testid="optimizer-loop-modal"')
    assert i != -1, "bloco do modal não encontrado"
    # janela do bloco: da ABERTURA da div raiz (o testid vem depois do
    # @keydown no mesmo tag) até o footer do modal
    start = src.rfind("<div", 0, i)
    end = src.find("</footer>", i)
    assert start != -1 and end != -1
    blk = src[start:end]
    assert '@keydown.escape.window="loopModalOpen=false"' in blk
    assert '@click="loopModalOpen=false"' in blk, "overlay não fecha no clique"
    assert blk.count('loopModalOpen=false') >= 3, "faltou botão Fechar"


def test_modal_hidden_by_default_with_cloak():
    """x-cloak + style display:none — padrão da plataforma para overlays
    (sem isso o modal pisca aberto no primeiro paint)."""
    src = _src()
    m = re.search(r'<div x-show="loopModalOpen"[^>]*>', src)
    assert m, "raiz do modal ausente"
    assert "x-cloak" in m.group(0)
    assert "display:none" in m.group(0)


# ── Abertura: automática no disparo + reabrível pelo card ─────────────
def test_start_loop_opens_modal_and_resets_previous_run():
    src = _src()
    m = re.search(r"async startLoop\(\)\{.*?\n    \},", src, re.S)
    assert m, "startLoop não encontrado"
    blk = m.group(0)
    assert "this.loopModalOpen=true" in blk, "startLoop não abre o modal"
    assert "this.loopRun=null" in blk and "this._prompts={}" in blk, (
        "startLoop não zera o estado do run anterior — o modal abriria "
        "mostrando a trilha do loop passado"
    )


def test_card_has_reopen_button():
    src = _src()
    assert 'data-testid="optimizer-loop-follow"' in src, "botão Acompanhar ausente"
    m = re.search(
        r'data-testid="optimizer-loop-follow"', src)
    before = src[:m.start()]
    assert '@click="loopModalOpen=true"' in src[m.start() - 300:m.end() + 50] or \
           '@click="loopModalOpen=true"' in before[-300:], \
        "botão Acompanhar não reabre o modal"


# ── Trilha derivada ───────────────────────────────────────────────────
def test_loop_trail_getter_covers_all_states():
    src = _src()
    m = re.search(r"get loopTrail\(\)\{.*?\n    \},", src, re.S)
    assert m, "getter loopTrail ausente"
    blk = m.group(0)
    for state in ("'done'", "'active'", "'pending'", "'skipped'"):
        assert state in blk, f"trilha sem o estado {state}"
    # etapas nomeadas do fluxo GEPA
    for key in ("'queue'", "'champion'", "'round'", "'holdout'", "'verdict'"):
        assert key in blk, f"trilha sem a etapa {key}"


def test_trail_renders_candidates_with_reflection_and_pareto():
    src = _src()
    m = re.search(
        r'data-testid="optimizer-loop-trail".*?</footer>', src, re.S)
    assert m, "corpo da trilha ausente"
    blk = m.group(0)
    assert 'x-text="c.reflection"' in blk, "reflexão do candidato não aparece"
    assert "✓pareto" in blk, "badge de pareto ausente"
    assert "viewPrompt(c)" in blk, "botão ver prompt ausente"


def test_view_prompt_fetches_candidate_detail_endpoint():
    src = _src()
    m = re.search(r"async viewPrompt\(c\)\{.*?\n    \},", src, re.S)
    assert m, "viewPrompt ausente"
    blk = m.group(0)
    assert "/candidates/" in blk, "viewPrompt não usa o endpoint de detalhe"
    # Footgun do proxy Alpine (memória da plataforma): mutar dict aninhado
    # in-place não dispara reatividade — precisa de NOVA referência.
    assert "{...this._prompts" in blk, (
        "viewPrompt muta _prompts in-place — atribua nova referência "
        "({...this._prompts, [id]: v}) para a reatividade do Alpine"
    )


# ── Veredito ──────────────────────────────────────────────────────────
def test_modal_has_verdict_block():
    src = _src()
    m = re.search(
        r'data-testid="optimizer-loop-verdict".*?</template>', src, re.S)
    assert m, "bloco de veredito ausente"
    blk = m.group(0)
    for marker in ("holdout_verdict", "stop_reason", "best_score",
                   "champion_score", "Histórico de revisões"):
        assert marker in blk, f"veredito sem {marker}"


def test_modal_footer_says_job_continues_server_side():
    """Fechar o modal NÃO cancela o job — o texto precisa dizer isso
    (convenção 'métricas sem falsa confiança': nada de UI ambígua)."""
    src = _src()
    assert "o job segue no servidor" in src, (
        "rodapé do modal não avisa que fechar não cancela o job"
    )
