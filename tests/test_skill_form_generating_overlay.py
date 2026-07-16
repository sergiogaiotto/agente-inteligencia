"""Smoke do template skill_form.html — feedback durante "Gerando SKILL.md…".

Achado no teste E2E (Cenário A): durante a geração da skill pelo wizard "IA, me
ajude", o editor SKILL.md PISCAVA com o template vazio (0 chars) antes de popular,
parecendo que falhou. Fix: um overlay com spinner cobre o textarea enquanto
`wizardLoading` está ativo, e o textarea fica desabilitado.

QA E2E 2026-07-16 (segunda rodada): o overlay não bastava — o usuário ficava
olhando a tela sem saber se estava funcionando (a chamada de LLM leva 10–40s e
o único feedback era o botão desabilitado). Fix: modal de progresso que narra
etapas com SINAL REAL (contexto montado / modelo escrevendo com cronômetro /
validando-aplicando) — sem porcentagem inventada, porque a geração é um único
POST sem streaming.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "skill_form.html").read_text(encoding="utf-8")


class TestGeneratingOverlay:
    def test_overlay_present(self, html):
        assert 'data-testid="skill-raw-generating"' in html
        assert "Gerando SKILL.md" in html

    def test_overlay_gated_by_wizard_loading(self, html):
        # o overlay só aparece durante a geração
        idx = html.index('data-testid="skill-raw-generating"')
        block = html[idx - 220: idx + 120]
        assert 'x-show="wizardLoading"' in block

    def test_textarea_disabled_while_generating(self, html):
        # o textarea não aceita digitação enquanto gera
        idx = html.index('data-testid="skill-raw"')
        block = html[idx: idx + 260]
        assert ':disabled="wizardLoading"' in block


class TestProgressModal:
    def _modal(self, html: str) -> str:
        idx = html.index('data-testid="wizard-progress-modal"')
        fim = html.index('tableBuilderOpen', idx)   # próximo modal = fim do bloco
        return html[idx - 300: fim]

    def test_modal_presente_e_gateado_pelo_loading(self, html):
        m = self._modal(html)
        assert 'x-show="wizardLoading"' in m
        assert "x-cloak" in m, "sem x-cloak o modal pisca no load da página"

    def test_acima_da_sidebar_e_dos_outros_modais(self, html):
        # sidebar/Query Builder usam até z-[60]; o progresso precisa vencer.
        assert "z-[70]" in self._modal(html)

    def test_cronometro_vivo(self, html):
        """O sinal de vida: segundos correndo provam que não travou."""
        m = self._modal(html)
        assert 'data-testid="wizard-progress-elapsed"' in m
        assert "wizardElapsed" in m

    def test_narra_as_tres_etapas_reais(self, html):
        m = self._modal(html)
        assert "Contexto montado" in m
        assert "O modelo está escrevendo o SKILL.md" in m
        assert "Validar e aplicar ao editor" in m

    def test_expectativa_honesta_sem_porcentagem(self, html):
        """A geração é um POST único sem streaming: prometer % seria inventar.
        O modal declara a expectativa real (10–40s) e avisa se passar de 60s."""
        m = self._modal(html)
        assert "de 10 a 40s" in m
        assert "wizardElapsed > 60" in m
        # nenhuma % fabricada exibida ao usuário (barra fake / contador fake)
        for fake in ("+ '%'", '+ "%"', "toFixed(0) + '%'", "progress-bar"):
            assert fake not in m, f"porcentagem fabricada no modal: {fake}"

    def test_js_conduz_e_limpa_o_timer(self, html):
        """O timer nasce no início da geração e morre SEMPRE (sucesso ou erro) —
        senão o intervalo vaza e o cronômetro do próximo run começa somado."""
        i = html.index("async runWizardSkill()")
        fn = html[i: i + 3200]
        assert "this.wizardStage = 2" in fn          # aguardando o LLM
        assert "this.wizardStage = 3" in fn          # resposta chegou
        assert "setInterval" in fn
        assert "clearInterval(this._wizardTimer)" in fn
        # o clear vem DEPOIS do catch (roda em sucesso E em erro)
        assert fn.index("catch") < fn.index("clearInterval")
