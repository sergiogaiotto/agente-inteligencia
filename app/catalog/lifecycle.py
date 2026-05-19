"""State machine do lifecycle de entries e do review de submissões.

Lifecycle da entry:
    draft → submitted → approved → published → deprecated → archived
                    ↘ rejected (volta a draft via re-submit)

Review da submission (independente do lifecycle da entry):
    pending → approved | rejected | changes_requested
"""

from __future__ import annotations

from typing import Iterable

# Estados terminais não permitem transição de saída.
ENTRY_STATES = (
    "draft",
    "submitted",
    "approved",
    "published",
    "deprecated",
    "archived",
)

# Grafo de transições válidas. Mapa: state → set de próximos states permitidos.
ENTRY_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "archived"}),
    "submitted": frozenset({"approved", "draft"}),  # 'draft' = changes_requested ou rejected
    "approved": frozenset({"published", "draft"}),
    "published": frozenset({"deprecated"}),
    "deprecated": frozenset({"published", "archived"}),  # pode revogar deprecação ou arquivar
    "archived": frozenset(),  # terminal
}

REVIEW_STATES = ("pending", "approved", "rejected", "changes_requested")

REVIEW_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "changes_requested"}),
    "approved": frozenset(),
    "rejected": frozenset(),
    "changes_requested": frozenset(),
}


def can_transition_entry(from_state: str, to_state: str) -> bool:
    """True se a transição entry from→to é permitida."""
    return to_state in ENTRY_TRANSITIONS.get(from_state, frozenset())


def can_transition_review(from_state: str, to_state: str) -> bool:
    """True se a transição review from→to é permitida."""
    return to_state in REVIEW_TRANSITIONS.get(from_state, frozenset())


def next_entry_states(from_state: str) -> Iterable[str]:
    """Lista próximos estados válidos para uma entry."""
    return sorted(ENTRY_TRANSITIONS.get(from_state, frozenset()))


def is_terminal_entry(state: str) -> bool:
    """True se o estado da entry não admite mais transições."""
    return not ENTRY_TRANSITIONS.get(state)
