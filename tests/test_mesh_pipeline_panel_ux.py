"""Painel do pipeline no Fluxo de agentes — 3 ajustes do QA E2E 2026-07-16.

1. Os textos explicativos de "Roteamento rápido" e "Auditoria da resposta"
   viraram popovers atrás de um "?" — o painel era um paredão de prosa que quem
   já sabe não relê. O texto NÃO sumiu: abre sob demanda.
2. O SELO do contrato não tinha lugar na tela: o usuário não sabia COMO selar.
   Agora o painel mostra 🔒 selado/🔓 não selado (da MESMA fonte que as
   integrações leem: /inputs-schema) e o "?" explica que → Publicado sela.
3. O domínio do pipeline (o chip "e2e" da lista) não tinha onde ser informado
   na UI — só via API. Agora é um campo editável no painel; vazio limpa
   (o PUT já coage "" → NULL).
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "pages" / "mesh_flow.html").read_text(encoding="utf-8")


class TestTextoViraPopover:
    @pytest.mark.parametrize("tid,tip", [
        ("help-fast-routing", "'fast'"),
        ("help-audit-posture", "'audit'"),
        ("help-seal", "'seal'"),
    ])
    def test_botao_interroga_toggle(self, html, tid, tip):
        i = html.index(f'data-testid="{tid}"')
        bloco = html[i - 260: i + 60]
        assert f"helpTip === {tip} ? null : {tip}" in bloco, f"{tid} sem toggle"

    def test_explicacoes_atras_do_x_show(self, html):
        """O texto continua existindo — mas só aparece sob demanda."""
        assert html.count("x-show=\"helpTip === 'fast'\"") == 1
        assert html.count("x-show=\"helpTip === 'audit'\"") == 1
        assert html.count("x-show=\"helpTip === 'seal'\"") == 1
        # o conteúdo não foi apagado
        assert "Pula a chamada LLM do agente de triagem" in html
        assert "Quem avalia a qualidade de cada etapa" in html

    def test_troca_de_pipeline_fecha_popover(self, html):
        i = html.index("selectPipeline(p) {")
        assert "this.helpTip = null" in html[i: i + 700]


class TestSeloDoContrato:
    def test_estado_do_selo_no_painel(self, html):
        i = html.index('data-testid="pipeline-seal"')
        bloco = html[i: i + 900]
        assert "sealInfo.sealed" in bloco
        assert "Contrato selado" in bloco and "Contrato não selado" in bloco
        assert "contract_version" in bloco

    def test_como_selar_esta_escrito(self, html):
        """A resposta à pergunta do usuário ('como selar?') está NA TELA."""
        i = html.index("x-show=\"helpTip === 'seal'\"")
        texto = html[i: i + 900]
        assert "Publicado" in texto
        assert "congela o contrato" in texto
        assert "drift" in texto
        assert "Frases-Prova" in texto   # o gate da publicação também

    def test_fonte_e_o_inputs_schema_com_guard(self, html):
        """Mesma fonte que Playground/integrações leem — e resposta atrasada de
        outro pipeline não pinta o selo do atual."""
        i = html.index("loadSealInfo(p) {")
        fn = html[i: i + 700]
        assert "/inputs-schema'" in fn
        assert "this.selectedPipeline.id === pid" in fn


class TestDominioEditavel:
    def test_campo_no_painel(self, html):
        i = html.index('data-testid="pipeline-domain-input"')
        bloco = html[i - 200: i + 400]
        assert 'setPipelineDomain($event.target.value)' in bloco
        assert "selectedPipeline.domain" in bloco

    def test_put_e_recarrega_lista(self, html):
        """O chip da lista vem do loadPipelines — sem recarregar, o painel e a
        lista divergem."""
        i = html.index("async setPipelineDomain(v) {")
        fn = html[i: i + 700]
        assert "api.put('/api/v1/pipelines/'" in fn
        assert "domain: domain" in fn
        assert "loadPipelines()" in fn

    def test_explica_para_que_serve(self, html):
        i = html.index('data-testid="pipeline-domain-input"')
        assert "chip na lista" in html[i: i + 800]
