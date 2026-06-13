"""State machine do lifecycle de pipelines (Estúdio de Pipelines).

Lifecycle de 3 estados (decisão travada no PLAN §3.2):
    rascunho  → publicado   (publicar)
    rascunho  → aposentado  (descartar)
    publicado → rascunho     (despublicar)
    publicado → aposentado  (aposentar)
    aposentado → publicado   (reativar)

Espelha o PADRÃO de app/catalog/lifecycle.py (frozenset de transições; funções
puras que retornam bool — o caller é quem levanta HTTPException). NÃO reusa os 6
estados do catálogo: pipeline é um ciclo simples e próprio. O mapeamento para os
6 estados do catálogo acontece só na publicação (Parte B), não aqui.
"""

from __future__ import annotations

from typing import Iterable

# Nenhum estado é terminal: 'aposentado' ainda pode ser reativado (→ publicado).
PIPELINE_STATES = (
    "rascunho",
    "publicado",
    "aposentado",
)

# Grafo de transições válidas. Mapa: state → set de próximos states permitidos.
PIPELINE_TRANSITIONS: dict[str, frozenset[str]] = {
    "rascunho": frozenset({"publicado", "aposentado"}),   # publicar ou descartar
    "publicado": frozenset({"rascunho", "aposentado"}),   # despublicar ou aposentar
    "aposentado": frozenset({"publicado"}),               # reativar
}


def can_transition_pipeline(from_state: str, to_state: str) -> bool:
    """True se a transição de pipeline from→to é permitida."""
    return to_state in PIPELINE_TRANSITIONS.get(from_state, frozenset())


def next_pipeline_states(from_state: str) -> Iterable[str]:
    """Lista próximos estados válidos para um pipeline (ordenado)."""
    return sorted(PIPELINE_TRANSITIONS.get(from_state, frozenset()))


def is_terminal_pipeline(state: str) -> bool:
    """True se o estado do pipeline não admite mais transições."""
    return not PIPELINE_TRANSITIONS.get(state)
