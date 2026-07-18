"""Cards de tipo de agente (Especialista/Triagem/Maestro) redesenhados.

User (2026-07-18): os descritivos não estavam bons (não-paralelos, "faz bem"
não diferencia, o "Maestro não executa" ficava escondido) e os exemplos
dinâmicos ("99 agente(s)", nomes reais) eram ruído. Trocamos por copy paralela
(Executa/Escolhe/Coordena) + selo de escopo, removemos o "Ex." do card e
movemos a orientação profunda para um "?" (popover: Escolha quando / Evite se /
Exemplo / Peso / Próximo passo + decodificação do badge).
"""
from __future__ import annotations

from pathlib import Path

import pytest

PG = Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "agent_form.html"


@pytest.fixture(scope="module")
def html() -> str:
    return PG.read_text(encoding="utf-8")


class TestCopyNova:
    def test_titulos_frase_paralelos(self, html):
        assert "Executa uma tarefa e responde." in html
        assert "Escolhe o especialista certo e encaminha." in html
        assert "Coordena vários agentes até a resposta final — delega, não executa." in html

    def test_selo_de_escopo(self, html):
        assert "'1 tarefa'" in html
        assert "'escolhe 1 de N'" in html
        assert "'N agentes · 1 fluxo'" in html
        assert 'x-text="card.scope"' in html


class TestExemploDinamicoRemovido:
    def test_card_example_sumiu(self, html):
        assert "cardExample(" not in html
        assert "card.example" not in html

    def test_textos_antigos_sumiram(self, html):
        for old in ("Faz uma coisa muito bem", "manda pro especialista certo",
                    "Rege vários especialistas", "são especialistas.", "mandar pro certo"):
            assert old not in html, f"copy antiga ainda presente: {old}"


class TestGuiaNoPopover:
    def test_afordancia_e_estado(self, html):
        assert "toggleGuide(card.kind)" in html
        assert "openGuide" in html
        assert "'agent-kind-help-' + card.kind" in html
        assert "'agent-kind-guide-' + card.kind" in html

    def test_estrutura_do_guia(self, html):
        for label in ("Escolha quando", "Evite se", "Exemplo", "Peso", "Próximo passo"):
            assert label in html, f"seção do guia faltando: {label}"
        assert 'x-text="card.guideTitle"' in html

    def test_decodifica_o_badge(self, html):
        assert "a camada técnica do agente." in html


class TestMentorRailIntacto:
    def test_intro_ainda_alimenta_o_rail(self, html):
        # o Mentor rail continua (complementar ao "?").
        assert "mentorIntroForKind" in html
