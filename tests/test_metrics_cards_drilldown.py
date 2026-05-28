"""Drilldown UI nos cards de Métricas do painel de Rastreabilidade.

User pediu (2026-05-28): cards de EVIDÊNCIAS / MCP TOOLS / API TOOLS
viraram clicáveis. Clique expande painel abaixo com lista dos itens
que compõem o contador.

Backend já expunha:
- trace.evidence_detail (PR #162): chunks RAG com score/source/preview
- trace.mcp_tools (PR pré-existente): invocações reais de MCP
- trace.api_tools_count (apenas contagem)

Este PR adiciona trace.api_tools (lista detalhada) e wire UI nos 3 cards.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def workspace_html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def engine_py() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "agents" / "engine.py").read_text(encoding="utf-8")


# ─── Backend: trace.api_tools expõe lista detalhada ────────────────


class TestBackendApiToolsExposure:
    def test_trace_dict_includes_api_tools_list(self, engine_py):
        """trace agora expõe `api_tools` (lista) além de `api_tools_count`."""
        assert '"api_tools":' in engine_py
        # Não-regressão: o count continua sendo exposto pra back-compat
        assert '"api_tools_count":' in engine_py

    def test_api_tools_invoked_captures_binding_details(self, engine_py):
        """Captura binding_id, status_code, latency, attempts — campos
        que a UI usa pra renderizar cada linha do painel expandido."""
        # Campo da estrutura precisa estar no código
        for field in ("binding_id", "status_code", "latency_ms", "attempts", "is_compensation"):
            assert field in engine_py


# ─── Frontend: Alpine state + bindings ─────────────────────────────


class TestAlpineState:
    def test_metric_expanded_state_initialized(self, workspace_html):
        """Estado `metricExpanded` é o controle do toggle — vazio por padrão."""
        assert "metricExpanded:''" in workspace_html or "metricExpanded: ''" in workspace_html


class TestMetricCardsClickable:
    def test_evidence_card_toggles_on_click(self, workspace_html):
        """Card de EVIDÊNCIAS tem @click que alterna metricExpanded."""
        # Confirma click handler com toggle
        assert "metricExpanded = metricExpanded === 'evidence' ? '' : 'evidence'" in workspace_html

    def test_mcp_card_toggles_on_click(self, workspace_html):
        assert "metricExpanded = metricExpanded === 'mcp' ? '' : 'mcp'" in workspace_html

    def test_api_card_toggles_on_click(self, workspace_html):
        assert "metricExpanded = metricExpanded === 'api' ? '' : 'api'" in workspace_html

    def test_cards_only_clickable_when_count_positive(self, workspace_html):
        """Clique gated por contagem > 0 — não confunde user com card vazio
        que abre painel vazio."""
        # Evidence: gated por evidence_count > 0
        assert "(lastTrace.trace?.evidence_count||0) > 0" in workspace_html
        # MCP: gated por mcp_tools.length > 0
        assert "(lastTrace.trace?.mcp_tools?.length||0) > 0" in workspace_html
        # API: gated por api_tools_count > 0
        assert "(lastTrace.trace?.api_tools_count||0) > 0" in workspace_html

    def test_chevron_indicates_expand_state(self, workspace_html):
        """Chevron ▾ (fechado) / ▴ (aberto) sinaliza visualmente o toggle."""
        # Pelo menos um caractere de cada estado aparece
        assert "▾" in workspace_html
        assert "▴" in workspace_html


class TestDrilldownPanels:
    def test_evidence_panel_renders_chunks(self, workspace_html):
        """Painel de evidência usa trace.evidence_detail com ordinal/score/source."""
        assert "lastTrace.trace?.evidence_detail" in workspace_html
        assert "ev.ordinal" in workspace_html
        assert "ev.score" in workspace_html

    def test_evidence_panel_color_codes_score(self, workspace_html):
        """Score >= 0.7 destaca verde, < 0.3 amber — coerente com PR #163
        (threshold da skill) que usa mesma escala."""
        assert "ev.score >= 0.7" in workspace_html
        assert "ev.score >= 0.3" in workspace_html

    def test_mcp_panel_renders_invocations(self, workspace_html):
        """Painel MCP mostra nome, server, status, latência da invocação real
        (não tools declaradas)."""
        assert "lastTrace.trace?.mcp_tools" in workspace_html
        assert "t.name" in workspace_html
        assert "t.server" in workspace_html
        assert "t.status" in workspace_html
        assert "t.latency_ms" in workspace_html

    def test_api_panel_renders_binding_executions(self, workspace_html):
        """Painel API mostra binding_id, status_code, latência, attempts e
        flag is_compensation — dados específicos de binding_executions."""
        assert "lastTrace.trace?.api_tools" in workspace_html
        assert "a.binding_id" in workspace_html
        assert "a.status_code" in workspace_html
        assert "a.is_compensation" in workspace_html

    def test_api_panel_shows_circuit_breaker_warning(self, workspace_html):
        """Quando binding foi skipped pelo circuit breaker, aparece destaque."""
        assert "skipped_by_breaker" in workspace_html
        assert "circuit-breaker" in workspace_html

    def test_panels_use_x_show_with_condition(self, workspace_html):
        """Painéis só renderizam quando o estado bate E há itens — evita
        painel vazio piscando."""
        assert "metricExpanded === 'evidence'" in workspace_html
        assert "metricExpanded === 'mcp'" in workspace_html
        assert "metricExpanded === 'api'" in workspace_html
