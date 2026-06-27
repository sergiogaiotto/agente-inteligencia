"""Resultado do "Executar" (runModal) renderizado de forma elegante.

Antes (achado de UX 2026-06-27): a saída do pipeline aparecia como JSON CRU num
<pre> (ex.: `{"interpretation":…,"conclusion":…}`) — feio e ilegível. Agora:
- saída JSON → cartões rotulados (Interpretation / Conclusion); texto → prosa;
- trilha "Como cheguei aqui" por agente (status + 💬 mensagem de status + duração),
  colapsável — usa os pipeline_steps que a resposta já traz.

Convenção: sem harness de DOM/Alpine — varredura de template.
"""
from pathlib import Path

MESH_FLOW = Path("app/templates/pages/mesh_flow.html")


def _src() -> str:
    return MESH_FLOW.read_text(encoding="utf-8")


def test_nao_despeja_mais_json_cru_no_pre():
    src = _src()
    # a regressão a travar: a saída final não pode voltar a ser um <pre> cru.
    assert '<pre class="whitespace-pre-wrap break-words text-[12px] text-surface-800 max-h-60 overflow-y-auto font-sans" x-text="runModal.result.output' not in src


def test_resposta_hero_json_para_cartoes_ou_texto():
    src = _src()
    assert 'data-testid="pipeline-run-answer"' in src
    assert "runOutputCards()" in src
    # os dois ramos: cartões (quando JSON) e prosa (quando texto)
    assert 'x-for="c in runOutputCards()"' in src
    assert 'x-text="runModal.result.output' in src  # fallback de texto


def test_trilha_por_agente_com_status_e_narrativa():
    src = _src()
    assert "Como cheguei aqui" in src
    assert 'data-testid="pipeline-run-trail-toggle"' in src
    assert "stepMeta(s)" in src
    # a narrativa (mensagem de status) por etapa aparece na trilha
    assert "s.status_message" in src
    # itera sobre os steps que a resposta já devolve
    assert 'x-for="(s, i) in runModal.result.pipeline_steps"' in src


def test_helpers_alpine_definidos():
    src = _src()
    assert "runOutputCards() {" in src
    assert "stepMeta(s) {" in src
    # estado do modal ganhou o toggle da trilha
    assert "showTrail: false" in src
