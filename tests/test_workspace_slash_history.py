"""Onda A.4 — Histórico/sugestões de slash invokes no workspace.

Tracking 100% client-side via localStorage (resiliente a modo privado de
alguns browsers, JSON corrompido, ausência de localStorage). Aparece como
seção "Recente" no topo do slash popover quando user não está filtrando
texto.

Cobertura (smoke estático do HTML/JS — Alpine.js não roda em pytest):
- Estado e constants no x-data
- Helpers de localStorage (_slashHistoryKey, _readSlashHistory, _writeSlashHistory)
- _trackSlashInvoke registra após sucesso de invokeBindingDirect
- _recentSlashCmds cruza com bindingsContext (filtra bindings sumidos)
- filteredSlashCmds prepend Recente quando SEM term de busca
- Dedup: binding em Recente não aparece de novo na lista geral
- Popover HTML: visual diferenciado (ícone clock, badge âmbar, gradiente)
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def workspace_html():
    return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")


# ────────────────────────────────────────────────────────────────
# State e constantes
# ────────────────────────────────────────────────────────────────


class TestSlashHistoryState:
    def test_storage_prefix_is_namespaced(self, workspace_html):
        """Chave de localStorage tem prefix que evita colisão com outras
        features (ex.: maestro.slash.recent.{agent_id})."""
        assert "_SLASH_HISTORY_PREFIX:" in workspace_html
        assert "maestro.slash.recent." in workspace_html

    def test_storage_limits_configured(self, workspace_html):
        """Cap de 20 entries totais; 5 visíveis no popover."""
        assert "_SLASH_HISTORY_LIMIT: 20" in workspace_html
        assert "_SLASH_HISTORY_SHOW: 5" in workspace_html


# ────────────────────────────────────────────────────────────────
# Helpers de localStorage
# ────────────────────────────────────────────────────────────────


class TestSlashHistoryHelpers:
    def test_has_key_getter(self, workspace_html):
        assert "_slashHistoryKey(agentId)" in workspace_html

    def test_has_read_helper(self, workspace_html):
        assert "_readSlashHistory(agentId)" in workspace_html
        # Defensive: filtra entries malformadas
        assert "e.kind && e.id && e.label" in workspace_html

    def test_has_write_helper(self, workspace_html):
        assert "_writeSlashHistory(agentId, entries)" in workspace_html

    def test_resilient_to_localstorage_failure(self, workspace_html):
        """Modo privado bloqueia localStorage — não pode crashar a UI."""
        # Read em try/catch
        assert "console.warn('slash history corrompida:'" in workspace_html
        # Write em try/catch
        assert "console.warn('slash history não salvou:'" in workspace_html


# ────────────────────────────────────────────────────────────────
# Tracker: _trackSlashInvoke
# ────────────────────────────────────────────────────────────────


class TestTrackSlashInvoke:
    def test_has_tracker(self, workspace_html):
        assert "_trackSlashInvoke(binding, skillId)" in workspace_html

    def test_tracks_timestamps_for_recency_window(self, workspace_html):
        """Janela rolling 7d via lista de timestamps — permite \"5x últimos 7d\"."""
        assert "oneWeek" in workspace_html
        assert "now - t < oneWeek" in workspace_html

    def test_dedups_by_kind_and_id(self, workspace_html):
        """Mesmo binding (kind+id) atualiza entry existente em vez de duplicar."""
        assert "binding.binding_kind && e.id === binding.binding_id" in workspace_html or \
               "e.kind === binding.binding_kind && e.id === binding.binding_id" in workspace_html

    def test_caps_timestamps_to_avoid_storage_bloat(self, workspace_html):
        """Power user pode invocar 1000 vezes — limitamos a 50 timestamps
        por entry. Janela 7d ainda fica correta pra contar."""
        assert "entry.timestamps.length > 50" in workspace_html

    def test_invoked_after_successful_invoke(self, workspace_html):
        """Tracker é chamado após sucesso de invokeBindingDirect.
        Defensivo: try/catch silencioso (rastreio falha não derruba UX)."""
        assert "this._trackSlashInvoke(b, skillId)" in workspace_html
        # Wrapped em try/catch
        assert "try{ this._trackSlashInvoke" in workspace_html


# ────────────────────────────────────────────────────────────────
# Getter: _recentSlashCmds
# ────────────────────────────────────────────────────────────────


class TestRecentGetter:
    def test_has_getter(self, workspace_html):
        assert "_recentSlashCmds()" in workspace_html

    def test_cross_references_with_bindings_context(self, workspace_html):
        """Recente só mostra bindings que ainda existem em bindingsContext —
        skill removida do agente, RAG source desautorizada → some do recent."""
        assert "ctxIndex" in workspace_html
        assert "bindingsContext" in workspace_html

    def test_marks_recent_entries(self, workspace_html):
        """Entry recente tem _isRecent=true pra UI diferenciar visualmente."""
        assert "_isRecent: true" in workspace_html


# ────────────────────────────────────────────────────────────────
# Integração com filteredSlashCmds
# ────────────────────────────────────────────────────────────────


class TestFilteredSlashCmdsIntegration:
    def test_prepend_when_no_search_term(self, workspace_html):
        """Recente aparece no topo quando user NÃO está filtrando texto."""
        assert "showingRecents" in workspace_html

    def test_dedupe_binding_in_recent_and_general(self, workspace_html):
        """Binding listado em Recente NÃO aparece de novo na lista geral —
        evita duplicidade visual."""
        assert "recentKeys" in workspace_html
        assert "recentKeys.has(dedupKey)" in workspace_html


# ────────────────────────────────────────────────────────────────
# Visual rendering
# ────────────────────────────────────────────────────────────────


class TestPopoverVisuals:
    def test_recent_entry_has_clock_icon(self, workspace_html):
        """Ícone de clock pra Recente (em vez do código /name)."""
        # Path SVG do clock (Heroicons outline clock)
        assert "M12 8v4l3 3" in workspace_html
        assert "cmd._isRecent" in workspace_html

    def test_recent_entry_has_amber_styling(self, workspace_html):
        """Badge âmbar diferencia Recente da seção principal (violeta/fuchsia)."""
        assert "bg-amber-100 text-amber-700" in workspace_html
        assert "from-amber-50/40" in workspace_html

    def test_recent_label_shown(self, workspace_html):
        """Span \"Recente\" ao lado do badge MCP/API/RAG/Tabular."""
        assert ">Recente<" in workspace_html

    def test_description_shows_frequency_for_repeat_invokes(self, workspace_html):
        """\"5x nos últimos 7d\" quando recent7d > 1; \"invocado antes\" quando == 1."""
        assert "nos últimos 7d" in workspace_html
        assert "invocado antes" in workspace_html


# ────────────────────────────────────────────────────────────────
# Resilience: bindings que somem
# ────────────────────────────────────────────────────────────────


class TestResilience:
    def test_skip_recent_entries_when_binding_no_longer_exists(self, workspace_html):
        """Skill removida do agente → entry no localStorage mas binding sumiu
        do bindingsContext. _recentSlashCmds pula (continue) em vez de
        renderizar item órfão."""
        # No corpo de _recentSlashCmds, há continue/if(!ctx) pra pular
        assert "if(!ctx) continue" in workspace_html

    def test_no_render_when_no_history(self, workspace_html):
        """Sem invocações anteriores, _recentSlashCmds retorna [] — seção
        Recente nem aparece."""
        assert "if(!history.length) return []" in workspace_html
