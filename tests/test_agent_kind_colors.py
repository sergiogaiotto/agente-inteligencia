"""Cores dos badges de kind (AOBD/AR/SA) — pedido do user.

Histórico:
- 2026-05-28: AOBD em vermelho (rose), AR em laranja (orange), SA em teal.
- 2026-06-13: AOBD passa a **slate (quase preto)** — padronização pedida pelo
  user (o Maestro/AOBD deve ser quase preto, no mesmo padrão das demais cores).

Mapa canônico atual: AOBD→slate (near-black), AR→orange, SA→teal.

Cobre as 3 telas que renderizam o kind do agente:
- agents.html (lista + preview lateral)
- workspace.html (avatar do chat + pipeline steps)
- settings.html (lista de system prompts)
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def agents_html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agents.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workspace_html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "workspace.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def settings_html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html").read_text(encoding="utf-8")


class TestAgentsListColors:
    def test_aobd_uses_slate_near_black(self, agents_html):
        """Avatar AOBD agora é slate (quase preto)."""
        # Avatar quadrado (lista)
        assert "agent.kind==='aobd'?'bg-slate-800'" in agents_html
        # Label text abaixo do nome
        assert "agent.kind==='aobd'?'text-slate-700'" in agents_html

    def test_router_uses_orange_not_brand(self, agents_html):
        """Avatar AR continua laranja."""
        assert "agent.kind==='router'?'bg-orange-500'" in agents_html
        assert "agent.kind==='router'?'text-orange-500'" in agents_html

    def test_subagent_keeps_teal(self, agents_html):
        """SA mantém teal."""
        assert "'bg-teal-500'" in agents_html
        assert "'text-teal-500'" in agents_html

    def test_preview_panel_uses_new_colors(self, agents_html):
        """Painel lateral de preview também usa slate para AOBD."""
        assert "previewAgent?.kind==='aobd'?'bg-slate-800'" in agents_html
        assert "previewAgent?.kind==='router'?'bg-orange-500'" in agents_html
        # Badge "AOBD — Orquestrador" no preview
        assert "previewAgent?.kind==='aobd'?'bg-slate-100 text-slate-700'" in agents_html
        assert "previewAgent?.kind==='router'?'bg-orange-50 text-orange-700'" in agents_html

    def test_aobd_no_longer_rose_or_violet(self, agents_html):
        """Não-regressão: AOBD não mapeia mais para rose nem violet."""
        assert "kind==='aobd'?'bg-rose" not in agents_html
        assert "kind==='aobd'?'text-rose" not in agents_html
        assert "agent.kind==='aobd'?'bg-violet-500'" not in agents_html


class TestWorkspaceAgentColors:
    def test_chat_avatar_uses_new_colors(self, workspace_html):
        """Badge do agente nas mensagens do chat — aobd=slate, router=orange."""
        assert "msg._agentKind==='aobd'?'bg-slate-100 text-slate-700'" in workspace_html
        assert "msg._agentKind==='router'?'bg-orange-50 text-orange-600'" in workspace_html

    def test_pipeline_step_uses_new_colors(self, workspace_html):
        """Step number no painel de pipeline — só quando status=completed.
        Status=error continua rose (caminho separado, não confundir com AOBD)."""
        assert "step.agent_kind==='aobd'?'bg-slate-200 text-slate-800'" in workspace_html
        assert "step.agent_kind==='router'?'bg-orange-100 text-orange-700'" in workspace_html

    def test_pipeline_step_badge_uses_new_colors(self, workspace_html):
        """Badge ao lado do nome do agente no step."""
        assert "step.agent_kind==='aobd'?'bg-slate-100 text-slate-700'" in workspace_html
        assert "step.agent_kind==='router'?'bg-orange-50 text-orange-600'" in workspace_html

    def test_failed_status_still_rose(self, workspace_html):
        """Não-regressão: status de falha continua rose (semântica de erro)."""
        assert "'bg-rose-100 text-rose-700'" in workspace_html


class TestSettingsSystemPromptColors:
    def test_system_prompt_avatar_uses_new_colors(self, settings_html):
        """Aba 'Prompts' em /settings — avatar do prompt por kind."""
        assert "p.kind==='aobd'?'bg-slate-100 text-slate-700'" in settings_html
        assert "p.kind==='router'?'bg-orange-100 text-orange-600'" in settings_html

    def test_system_prompt_label_uses_new_colors(self, settings_html):
        """Label uppercase do kind abaixo do nome do prompt."""
        assert "p.kind==='aobd'?'text-slate-700'" in settings_html
        assert "p.kind==='router'?'text-orange-500'" in settings_html
