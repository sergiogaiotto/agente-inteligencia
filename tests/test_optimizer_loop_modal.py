"""Modal de acompanhamento da Otimização automática (item 1, 50.0.0).

Ao clicar em "Otimizar automaticamente" o usuário acompanha o loop num MODAL
na própria tela de Avaliação: trilha na fila → champion → rodadas
(candidatos com score/pareto/reflexão + prompt sob demanda) → holdout →
veredito. Fechável (o job segue no servidor) e reabrível pelo card.

A revisão adversarial do próprio PR (6 achados de máquina de estados)
endureceu a trilha: 'active' só com evidência; run falhado nunca pinta
verde; holdout não-executado é 'skipped' com motivo — os testes daqui
travam exatamente esses contratos.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "app" / "templates" / "pages" / "harness.html"


def _src() -> str:
    return HARNESS.read_text(encoding="utf-8")


def _modal_block(src: str | None = None) -> str:
    """Bloco do modal: da abertura da div raiz até o footer — âncora pelo
    testid mas recua até o `<div` dono (atributos vêm em qualquer ordem)."""
    src = src or _src()
    i = src.find('data-testid="optimizer-loop-modal"')
    assert i != -1, "modal ausente (data-testid=optimizer-loop-modal)"
    start = src.rfind("<div", 0, i)
    end = src.find("</footer>", i)
    assert start != -1 and end != -1, "bloco do modal malformado"
    return src[start:end]


def _trail_getter(src: str | None = None) -> str:
    src = src or _src()
    m = re.search(r"get loopTrail\(\)\{.*?\n    \},", src, re.S)
    assert m, "getter loopTrail ausente"
    return m.group(0)


# ── Estrutura do modal ────────────────────────────────────────────────
def test_modal_is_accessible_dialog():
    blk = _modal_block()
    # [review 9] role/aria SÓ contam dentro do bloco do modal (o drawer do
    # caso também tem role=dialog no arquivo — assert global é tautológico).
    assert 'role="dialog"' in blk
    assert 'aria-modal="true"' in blk
    assert 'tabindex="-1"' in blk
    assert "$el.focus()" in blk, "modal não recebe foco ao abrir"


def test_modal_root_hidden_by_default_any_attr_order():
    """[review 12] regex por lookaheads — não depende da ordem dos atributos."""
    src = _src()
    m = re.search(
        r'<div(?=[^>]*x-show="loopModalOpen")(?=[^>]*x-cloak)'
        r'(?=[^>]*display:none)(?=[^>]*data-testid="optimizer-loop-modal")[^>]*>',
        src,
    )
    assert m, "raiz do modal sem x-show+x-cloak+display:none (pisca no paint)"


def test_modal_above_case_drawer():
    """[review 7] modal z-[9995] > drawer z-[9990] — se ambos abrirem, a
    tarefa em foco (modal) vence."""
    blk = _modal_block()
    assert "z-[9995]" in blk, "modal perdeu o z-index acima do drawer"
    assert "z-[9990]" in _src(), "drawer mudou de camada — reavalie o modal"


def test_modal_inside_optimizer_loop_scope():
    """O modal PRECISA estar dentro do x-data optimizerLoop. Checagem
    textual: aparece depois do card, antes do banner do drawer, e usa
    bindings que SÓ existem nesse escopo (loopTrail/loopRun)."""
    src = _src()
    card = src.find('data-testid="optimizer-loop-card"')
    modal = src.find('data-testid="optimizer-loop-modal"')
    drawer_banner = src.find("DRAWER do caso")
    assert card != -1 and modal != -1 and drawer_banner != -1
    assert card < modal < drawer_banner, (
        "modal fora da região do card do optimizerLoop — precisa ser filho "
        "do x-data (fora dele loopRun/loopTrail não existem)"
    )
    blk = _modal_block(src)
    assert "loopTrail" in blk and "loopRun" in blk, (
        "modal não usa o estado do optimizerLoop — foi movido de escopo?"
    )


def test_modal_closes_by_escape_overlay_and_footer_button():
    blk = _modal_block()
    assert '@keydown.escape.window="loopModalOpen=false"' in blk
    assert '@click="loopModalOpen=false"' in blk, "overlay não fecha no clique"
    # [review 11] o botão Fechar do FOOTER é asserted no próprio footer —
    # escape+overlay+X do header já somavam 3 e mascaravam a ausência.
    m = re.search(r"<footer.*$", blk, re.S)
    assert m and 'loopModalOpen=false' in m.group(0), "footer sem botão Fechar"


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
    i = src.find('data-testid="optimizer-loop-follow"')
    assert i != -1, "botão Acompanhar ausente"
    # [review 13] janela única em volta do botão (o OR anterior era morto)
    window = src[max(0, i - 300):i + 100]
    assert '@click="loopModalOpen=true"' in window, (
        "botão Acompanhar não reabre o modal"
    )


# ── Máquina de estados da trilha ──────────────────────────────────────
def test_trail_covers_five_states():
    blk = _trail_getter()
    for state in ("'done'", "'active'", "'pending'", "'skipped'", "'error'"):
        assert state in blk, f"trilha sem o estado {state}"
    for key in ("'queue'", "'champion'", "'round'", "'holdout'", "'verdict'"):
        assert key in blk, f"trilha sem a etapa {key}"


def test_trail_no_phantom_active_round():
    """[review 1/6] rodada SEM candidatos em run 'running' fica 'pending' —
    o dado polled não distingue 'propondo' de 'holdout pós-parada', então a
    trilha não inventa um active."""
    blk = _trail_getter()
    # o único caminho p/ 'active' de rodada exige candidatos inseridos
    m = re.search(r"if\(cands\.length\)\{(.*?)\}else", blk, re.S)
    assert m and "'active'" in m.group(1), "active de rodada sem evidência"
    after = blk.split("else if(terminal)")[1]
    assert "st='pending'" in after.split("steps.push")[0], (
        "rodada vazia em run running deveria ser 'pending'"
    )
    assert "champion&&i===roundsDone+1)?'active'" not in blk, (
        "voltou o active fantasma de rodada vazia (achado [1] da revisão)"
    )


def test_trail_crashed_run_never_paints_green():
    """[review 2/4/5] failed/timeout/interrupted: rodada parcial = 'error',
    vazia = 'skipped' com o motivo REAL, veredito nunca 'done' sem result."""
    blk = _trail_getter()
    assert "crashed=['failed','timeout','interrupted']" in blk.replace(" ", ""), \
        "getter perdeu a noção de run crashed"
    assert "interrompida no meio — o run terminou" in blk
    assert "não executada — o run terminou" in blk
    assert "parada antecipada por paciência/teto" in blk, (
        "legenda de early-stop não distingue crash de parada deliberada"
    )
    # veredito: done SÓ com result; crash → 'error'
    m = re.search(r"key:'verdict'.*?state:(.*?)\}\);", blk, re.S)
    assert m, "passo verdict ausente"
    assert "res?'done'" in m.group(1).replace(" ", ""), (
        "veredito 'done' sem exigir result — run falhado pintaria verde"
    )
    assert "'error'" in m.group(1), "veredito de run crashed não vira 'error'"


def test_trail_holdout_skipped_when_not_executed():
    """[review 3] 'sem_holdout' e 'nao_confirmado_sem_ganho' = a avaliação
    de holdout NÃO rodou → 'skipped' com explicação, nunca verde."""
    blk = _trail_getter()
    assert "'sem_holdout'" in blk and "'nao_confirmado_sem_ganho'" in blk, (
        "getter não trata os vereditos de holdout não-executado"
    )
    assert "holdoutSkipped?'skipped'" in blk.replace(" ", ""), (
        "holdout não-executado deveria ser 'skipped'"
    )
    assert "gold sem split de holdout" in blk
    assert "nenhuma variante superou o champion no treino" in blk


def test_trail_renders_error_state_in_rose():
    src = _src()
    blk = _modal_block(src)
    assert "'bg-rose-400'" in blk, "bolinha do estado error não é rose"
    assert "'text-rose-700'" in blk, "label do estado error não é rose"


# ── Candidatos + prompt sob demanda ───────────────────────────────────
def test_trail_renders_candidates_with_reflection_and_pareto():
    blk = _modal_block()
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
    assert "o job segue no servidor" in _src(), (
        "rodapé do modal não avisa que fechar não cancela o job"
    )
