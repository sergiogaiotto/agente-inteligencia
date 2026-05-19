"""Testes da state machine de lifecycle (entry + review)."""

from __future__ import annotations

from app.catalog.lifecycle import (
    ENTRY_STATES,
    ENTRY_TRANSITIONS,
    REVIEW_STATES,
    REVIEW_TRANSITIONS,
    can_transition_entry,
    can_transition_review,
    is_terminal_entry,
    next_entry_states,
)


class TestEntryTransitions:
    def test_happy_path_publish(self):
        # draft → submitted → approved → published
        assert can_transition_entry("draft", "submitted")
        assert can_transition_entry("submitted", "approved")
        assert can_transition_entry("approved", "published")

    def test_published_can_be_deprecated(self):
        assert can_transition_entry("published", "deprecated")

    def test_deprecated_can_be_republished_or_archived(self):
        assert can_transition_entry("deprecated", "published")
        assert can_transition_entry("deprecated", "archived")

    def test_archived_is_terminal(self):
        assert is_terminal_entry("archived")
        # Nenhuma transição sai de archived
        for target in ENTRY_STATES:
            assert not can_transition_entry("archived", target)

    def test_cant_skip_states(self):
        assert not can_transition_entry("draft", "approved")
        assert not can_transition_entry("draft", "published")
        assert not can_transition_entry("submitted", "published")
        assert not can_transition_entry("approved", "deprecated")

    def test_cant_go_backwards_arbitrarily(self):
        # published não pode voltar para draft direto
        assert not can_transition_entry("published", "draft")
        # approved não pode voltar para submitted
        assert not can_transition_entry("approved", "submitted")

    def test_submitted_back_to_draft_on_rejection(self):
        # changes_requested ou rejected → publisher itera no draft
        assert can_transition_entry("submitted", "draft")

    def test_approved_back_to_draft(self):
        # Permite editar pós-aprovação (re-submit ciclo)
        assert can_transition_entry("approved", "draft")

    def test_unknown_state_rejects_everything(self):
        assert not can_transition_entry("bogus", "draft")

    def test_next_entry_states_sorted(self):
        # next_entry_states retorna lista ordenada
        nxt = list(next_entry_states("draft"))
        assert nxt == sorted(nxt)
        assert set(nxt) == ENTRY_TRANSITIONS["draft"]

    def test_all_states_documented(self):
        # Garante que cada state em ENTRY_STATES tem entrada em TRANSITIONS
        for s in ENTRY_STATES:
            assert s in ENTRY_TRANSITIONS


class TestReviewTransitions:
    def test_pending_can_become_any_decision(self):
        for decision in ("approved", "rejected", "changes_requested"):
            assert can_transition_review("pending", decision)

    def test_decisions_are_terminal(self):
        for decision in ("approved", "rejected", "changes_requested"):
            assert REVIEW_TRANSITIONS[decision] == frozenset()
            for target in REVIEW_STATES:
                assert not can_transition_review(decision, target)

    def test_cant_revert_to_pending(self):
        for state in ("approved", "rejected", "changes_requested"):
            assert not can_transition_review(state, "pending")


class TestTerminalDetection:
    def test_only_archived_is_terminal(self):
        for s in ENTRY_STATES:
            if s == "archived":
                assert is_terminal_entry(s)
            else:
                assert not is_terminal_entry(s)
