"""Testes da máquina de estados do lifecycle de pipelines (PR1).

Funções puras (sem DB, sem HTTP) — espelha tests/test_catalog_lifecycle.py.
3 estados: rascunho ⇄ publicado, publicado → aposentado, aposentado → publicado,
rascunho → aposentado.
"""

from app.agents.pipeline_lifecycle import (
    PIPELINE_STATES,
    PIPELINE_TRANSITIONS,
    can_transition_pipeline,
    next_pipeline_states,
    is_terminal_pipeline,
)


class TestPipelineTransitions:
    def test_publicar_e_despublicar(self):
        assert can_transition_pipeline("rascunho", "publicado")
        assert can_transition_pipeline("publicado", "rascunho")

    def test_descartar_rascunho(self):
        assert can_transition_pipeline("rascunho", "aposentado")

    def test_aposentar_publicado(self):
        assert can_transition_pipeline("publicado", "aposentado")

    def test_reativar_aposentado(self):
        assert can_transition_pipeline("aposentado", "publicado")

    def test_aposentado_nao_volta_direto_a_rascunho(self):
        # aposentado só pode reativar (→ publicado); rascunho não é permitido.
        assert not can_transition_pipeline("aposentado", "rascunho")

    def test_nao_transita_para_si_mesmo(self):
        for s in PIPELINE_STATES:
            assert not can_transition_pipeline(s, s)

    def test_estado_desconhecido_rejeita_tudo(self):
        assert not can_transition_pipeline("bogus", "publicado")
        for target in PIPELINE_STATES:
            assert not can_transition_pipeline("bogus", target)

    def test_next_states_ordenado_e_consistente(self):
        nxt = list(next_pipeline_states("rascunho"))
        assert nxt == sorted(nxt)
        assert set(nxt) == PIPELINE_TRANSITIONS["rascunho"]
        assert set(next_pipeline_states("aposentado")) == {"publicado"}

    def test_nenhum_estado_e_terminal(self):
        # Todos têm ao menos uma transição de saída (aposentado → publicado).
        for s in PIPELINE_STATES:
            assert not is_terminal_pipeline(s)

    def test_terminal_para_estado_desconhecido(self):
        assert is_terminal_pipeline("bogus")
