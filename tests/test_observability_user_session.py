"""Smoke do template observability.html — sessão atual e filtro de usuário.

User pediu (2026-05-31):
1. Em "Manutenção de Logs", incluir o usuário da sessão atual no header
   (operador precisa confirmar quem está logado antes de ações destrutivas
   como Forçar rotação / Limpar archives — exigem role=root).
2. No Log Viewer, novo filtro `userFilter` (select dinâmico populado pelos
   user_ids distintos da janela carregada).

Histórico: junto, corrigimos o bug em `_resolve_user_id` que lia cookie
`session` (inexistente) em vez de `user_id`. Sem essa correção o filtro
ficaria sem dados — todos os logs viriam com user_id vazio.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    path = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "observability.html"
    return path.read_text(encoding="utf-8")


class TestSessionHeaderInLogsMaintenance:
    """Cabeçalho 'Sessão' no card de Manutenção de Logs."""

    def test_session_label_present(self, html):
        """Header mostra rótulo 'Sessão:' quando há usuário logado."""
        assert "Sessão:" in html

    def test_session_chip_uses_current_user(self, html):
        """Chip exibe display_name / username / email como fallback chain."""
        assert "currentUser?.display_name || currentUser?.username || currentUser?.email" in html

    def test_session_role_badge_highlights_root(self, html):
        """Role badge: rose para root, emerald para demais (visual cue forte)."""
        assert "(currentUser?.role||'').toLowerCase()==='root'" in html

    def test_session_anonymous_fallback_present(self, html):
        """Quando não há sessão, mostra 'Sessão: anônima' (não esconde silenciosamente)."""
        assert "Sessão: anônima" in html

    def test_loads_me_endpoint_on_init(self, html):
        """`load()` chama /api/v1/users/me para popular currentUser."""
        assert "/api/v1/users/me" in html
        assert "this.currentUser = me?.user || null" in html


class TestUserFilterInLogViewer:
    """Select dinâmico de filtro por user_id no toolbar do Log Viewer."""

    def test_user_filter_state_initialized(self, html):
        """logView.userFilter começa vazio (sem filtro = todos)."""
        assert "userFilter: ''," in html

    def test_user_select_present_in_toolbar(self, html):
        """Select 'Todos usuários' aparece junto aos demais filtros."""
        assert "Todos usuários" in html
        assert 'x-model="logView.userFilter"' in html

    def test_user_select_populated_by_log_user_ids(self, html):
        """Options vêm do getter logUserIds (igual ao padrão de loggers/eventos)."""
        assert 'x-for="uid in logUserIds"' in html

    def test_log_user_ids_getter_collects_distinct_uids(self, html):
        """Getter percorre logView.parsed e coleta json.user_id distintos."""
        assert "get logUserIds()" in html
        assert "l.json?.user_id" in html

    def test_user_label_helper_present(self, html):
        """Helper userLabel(uid) formata UUID → 'nome (uuid8)' usando userIdToName."""
        assert "userLabel(uid)" in html
        assert "userIdToName" in html

    def test_user_filter_applied_in_log_filtered(self, html):
        """logFiltered respeita v.userFilter (filtro por igualdade exata de UUID)."""
        assert "v.userFilter && j?.user_id !== v.userFilter" in html

    def test_user_filter_resets_with_clear_filters(self, html):
        """Botão 'Limpar filtros' zera userFilter junto com os demais."""
        assert "this.logView.userFilter = ''" in html

    def test_user_filter_counts_as_active(self, html):
        """hasActiveFilters considera userFilter (mostra o botão 'Limpar')."""
        # A linha do getter inclui v.userFilter na disjunção
        assert "v.userFilter" in html
