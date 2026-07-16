"""Fase 2 do mesh — "Converse com seu agente" no Dossiê (QA E2E 2026-07-16).

Chat REAL com o agente selecionado no Fluxo de agentes, estilo ChatGPT, via
POST /api/v1/workspace/chat (cookie; mesma FSM/guardrails do runtime). Por que
NÃO /agents/{id}/invoke: aquele endpoint tem gate de exposição que 403a
subagentes de pipeline — correto para API externa, errado para a UI conversar
com o próprio nó. Multi-turn: o interaction_id devolvido vira o session_id do
próximo turno.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "pages" / "mesh_flow.html").read_text(encoding="utf-8")


class TestEntradas:
    def test_botao_no_dossie(self, html):
        i = html.index('data-testid="dossier-chat-btn"')
        assert '@click="openChat(selected)"' in html[i - 200: i + 100]

    def test_item_no_menu_de_contexto(self, html):
        i = html.index('data-testid="ctx-chat"')
        assert "openChat(ctxMenu.node)" in html[i - 120: i + 60]


class TestPainelDoChat:
    def _panel(self, html: str) -> str:
        i = html.index('data-testid="mesh-chat-panel"')
        return html[i - 400: html.index("curl_auth_modal", i)]

    def test_x_if_e_nao_x_show(self, html):
        """template x-if: os bindings de chat.* só existem com o chat aberto —
        x-show avaliaria chat.agent.name com chat=null e quebraria a página."""
        p = self._panel(html)
        assert '<template x-if="chat">' in p

    def test_backdrop_nao_fecha_no_clique(self, html):
        """Conversa não pode se perder por um clique acidental no backdrop —
        fechar é gesto explícito (X ou Esc)."""
        p = self._panel(html)
        assert "@click.self" not in p
        assert '@keydown.escape.window="closeChat()"' in p

    def test_chip_fsm_por_resposta(self, html):
        p = self._panel(html)
        assert 'data-testid="chat-fsm-chip"' in p
        assert "chatStateChip(m.state)" in p

    def test_enter_envia_e_shift_enter_quebra(self, html):
        p = self._panel(html)
        assert '@keydown.enter.exact.prevent="sendChat()"' in p

    def test_disabled_coagido_com_bang_bang(self, html):
        """Footgun Alpine da casa: :disabled com undefined vira atributo
        PRESENTE → botão morto. Coagir com !!."""
        p = self._panel(html)
        assert ':disabled="!!chat.sending' in p

    def test_resposta_do_agente_renderiza_markdown_sanitizado(self, html):
        p = self._panel(html)
        assert 'x-html="_md(m.text)"' in p


class TestSendChat:
    def _fn(self, html: str) -> str:
        i = html.index("async sendChat()")
        return html[i: i + 2200]

    def test_usa_workspace_chat_modo_agente(self, html):
        fn = self._fn(html)
        assert "'/api/v1/workspace/chat'" in fn
        assert "mode: 'agent'" in fn

    def test_multi_turn_reusa_interaction_id_como_session(self, html):
        fn = self._fn(html)
        assert "r.interaction_id || c.session" in fn
        assert "session_id: c.session" in fn

    def test_guard_de_chat_trocado_durante_o_await(self, html):
        """Fechar/trocar o chat com uma resposta em voo não pode escrever numa
        conversa que não existe mais (nem reviver o spinner)."""
        fn = self._fn(html)
        assert "this.chat !== c" in fn
        assert "this.chat === c" in fn

    def test_nova_conversa_zera_a_sessao(self, html):
        i = html.index("resetChat() {")
        fn = html[i: i + 260]
        assert "session = null" in fn

    def test_estados_fsm_no_chip(self, html):
        i = html.index("chatStateChip(s)")
        fn = html[i: i + 500]
        for estado in ("Recommend", "Refuse", "Escalate"):
            assert estado in fn
