"""Mesh — "Conhecer o agente" no Dossiê / Menu de Regência.

Assistente que EXPLICA o agente (o que faz, propósito, config, posição no mesh,
comportamento agregado) via POST /api/v1/agents/{id}/explain. **NÃO executa** o
agente: sem interação, sem gasto do orçamento dele, sem histórico. Testar de
verdade fica no Playground / botão Executar.
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
        assert '@click="openChat(selected)"' in html[i - 200: i + 200]

    def test_item_no_menu_de_contexto(self, html):
        i = html.index('data-testid="ctx-chat"')
        assert "openChat(ctxMenu.node)" in html[i - 120: i + 60]

    def test_rotulo_conhecer_nao_conversar(self, html):
        # o rótulo deixa claro: CONHECER (entender), não "Conversar" (executar)
        assert "Conhecer o agente" in html
        assert "Conversar com o agente" not in html


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
        p = self._panel(html)
        assert "@click.self" not in p
        assert '@keydown.escape.window="closeChat()"' in p

    def test_badge_nao_executa_e_sem_chip_fsm(self, html):
        """Deixa EXPLÍCITO que não executa e NÃO mostra estado de FSM (não há
        execução): sem o chip de estado, com o selo 'não executa'."""
        p = self._panel(html)
        assert "não executa" in p
        assert 'data-testid="chat-fsm-chip"' not in p

    def test_atalho_para_o_playground(self, html):
        # para testar de verdade, o painel aponta o Playground
        p = self._panel(html)
        assert "/mesh/playground" in p

    def test_enter_envia_e_shift_enter_quebra(self, html):
        p = self._panel(html)
        assert '@keydown.enter.exact.prevent="sendChat()"' in p

    def test_disabled_coagido_com_bang_bang(self, html):
        """Footgun Alpine da casa: :disabled com undefined vira atributo
        PRESENTE → botão morto. Coagir com !!."""
        p = self._panel(html)
        assert ':disabled="!!chat.sending' in p

    def test_resposta_renderiza_markdown_sanitizado(self, html):
        p = self._panel(html)
        assert 'x-html="_md(m.text)"' in p


class TestSendChat:
    def _fn(self, html: str) -> str:
        i = html.index("async sendChat()")
        return html[i: i + 2200]

    def test_usa_explain_e_nao_executa(self, html):
        fn = self._fn(html)
        assert "'/api/v1/agents/' + c.agent.id + '/explain'" in fn
        # a UI de "conhecer" NUNCA executa o agente:
        assert "workspace/chat" not in fn
        assert "mode: 'agent'" not in fn

    def test_stateless_envia_history(self, html):
        """Servidor stateless: a UI manda o histórico recente (zero efeito
        colateral — sem interaction_id/session do agente)."""
        fn = self._fn(html)
        assert "history" in fn
        assert "interaction_id" not in fn

    def test_guard_de_chat_trocado_durante_o_await(self, html):
        """Fechar/trocar o chat com uma resposta em voo não pode escrever numa
        conversa que não existe mais (nem reviver o spinner)."""
        fn = self._fn(html)
        assert "this.chat !== c" in fn
        assert "this.chat === c" in fn

    def test_nova_conversa_limpa_as_mensagens(self, html):
        i = html.index("resetChat() {")
        fn = html[i: i + 260]
        assert "msgs = []" in fn
