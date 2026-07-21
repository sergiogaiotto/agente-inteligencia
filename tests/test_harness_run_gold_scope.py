"""Escopo do gold no run do harness (66.4.0) — achado do E2E de 2026-07-21.

Footgun reproduzido na VPS: o seletor "— versão —" da coluna Golden Dataset é
só FILTRO de listagem, mas o formulário "+ Executar" disparava SEMPRE com
``gold_version:'latest'`` (TODOS os datasets, teto 500 casos) sem nenhum
controle visível — um run acidental de 70 casos cross-domínio, síncrono e sem
cancelamento (DELETE recusa status 'running' por design).

Contrato selado aqui (o template é JS de Alpine — pytest não executa; o teste
sela os marcadores no source, padrão de test_gold_split_probe.py):
 1. o form tem seletor PRÓPRIO de dataset (``harness-goldversion``) ligado a
    ``runForm.gold_version``;
 2. o filtro da coluna Golden ESPELHA no runForm (change → mirror) e limpar o
    filtro desfaz o espelho;
 3. disparar com 'latest' havendo 2+ datasets pede confirmação via uiConfirm
    (nunca window.confirm — ver test_no_native_confirm_dialogs).
"""

from pathlib import Path

SRC = Path("app/templates/pages/harness.html").read_text(encoding="utf-8")


def test_run_form_tem_seletor_de_gold_visivel():
    assert 'data-testid="harness-goldversion"' in SRC
    assert 'x-model="runForm.gold_version"' in SRC
    # A opção do run-tudo é explícita sobre o que significa (sem eufemismo).
    assert '<option value="latest">todos os datasets</option>' in SRC


def test_filtro_do_golden_espelha_no_runform():
    # change do filtro → espelho ('' volta a 'latest'); o form segue editável.
    assert ("@change=\"runForm.gold_version = goldFilter.version || 'latest'\""
            in SRC)
    # limpar o filtro desfaz o espelho — senão o form guardaria um dataset
    # fantasma que a lista já não mostra.
    compact = SRC.replace(" ", "")
    assert "clearGoldFilter(){" in compact
    assert "this.runForm.gold_version='latest'}" in compact


def test_latest_com_multiplos_datasets_pede_confirmacao():
    # A guarda vive DENTRO de executeHarness, antes do POST.
    start = SRC.index("async executeHarness()")
    end = SRC.index("api.post('/api/v1/eval-runs/execute'", start)
    guard = SRC[start:end]
    assert "runForm.gold_version==='latest'" in guard
    assert "goldVersions.length>1" in guard
    assert "uiConfirm(" in guard
    # Cancelou → NÃO dispara (return antes do POST).
    assert "if(!ok)return" in guard.replace(" ", "")
