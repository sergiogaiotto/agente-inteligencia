"""Guard-rails dos achados A2A-2/A2A-3 (bateria E2E #4 "Atlas + A2A", 2026-07-09)
na tela /federation — feedback do invoke remoto e save de configuração.

A2A-2 (invoke sem feedback): o toast de erro EXISTIA mas é transiente (6–15s) e a
latência do invoke varia — na bateria o erro passou invisível. O fix mantém o
toast e adiciona uma área de erro PERSISTENTE inline (`e._error`) abaixo do campo,
no padrão do Playground; o resultado de sucesso ganha data-testid p/ automação.

A2A-3 (1º save "não pegou"): DOIS mecanismos combinados, ambos reproduzidos:
  1. o badge Ativa/Desligada lia `config.enabled` (estado LOCAL não salvo) — o
     operador via "Ativa" só de marcar o checkbox, antes de salvar;
  2. a resposta tardia do GET /config substitui `config` inteiro e DESFAZIA
     silenciosamente um tick dado durante o carregamento → o Salvar seguinte
     gravava o valor VELHO com toast de sucesso.
O fix: badge lê `savedEnabled` (estado confirmado pelo servidor) + chip
"alteração não salva", e o form fica `:disabled="!configLoaded"` até o GET chegar.

Se um teste aqui falhar, NÃO volte o badge para `config.enabled` nem destrave o
form antes do load — reintroduz a falsa confiança e a corrida.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FEDERATION = REPO_ROOT / "app" / "templates" / "pages" / "federation.html"


def _content() -> str:
    return FEDERATION.read_text(encoding="utf-8")


class TestRemoteInvokeFeedback:
    """A2A-2 — erro persistente inline + toast + área de resultado."""

    def test_inline_error_area_exists(self):
        content = _content()
        assert 'data-testid="remote-invoke-error"' in content, (
            "federation.html sem a área de erro inline do invoke remoto"
        )
        assert 'x-text="e._error"' in content, (
            "área de erro não renderiza e._error"
        )

    def test_catch_sets_inline_error_and_toast(self):
        content = _content()
        assert "e._error = err.message" in content, (
            "invokeRemote deve gravar o erro em e._error (feedback persistente)"
        )
        assert "showToast(e._error, 'error')" in content, (
            "invokeRemote deve manter o toast de erro além da área inline"
        )

    def test_error_cleared_on_retry(self):
        """Nova tentativa limpa o erro anterior (senão erro velho + spinner)."""
        assert "e._result = null; e._error = ''" in _content()

    def test_result_area_has_testid_and_output(self):
        content = _content()
        assert 'data-testid="remote-invoke-result"' in content
        assert 'x-text="e._result?.output"' in content


class TestConfigSaveFeedback:
    """A2A-3 — badge = estado salvo; form travado até o config carregar."""

    def test_badge_reads_saved_state_not_checkbox(self):
        content = _content()
        assert "x-text=\"savedEnabled ? 'Ativa' : 'Desligada'\"" in content, (
            "o badge deve ler savedEnabled (confirmado pelo servidor)"
        )
        assert "config.enabled ? 'Ativa'" not in content, (
            "regressão: badge otimista lendo o checkbox não salvo (A2A-3)"
        )

    def test_unsaved_hint_chip(self):
        assert 'data-testid="federation-unsaved-hint"' in _content(), (
            "sem o chip 'alteração não salva' o operador não distingue "
            "estado editado de estado salvo"
        )

    def test_form_gated_until_config_loads(self):
        content = _content()
        assert content.count(':disabled="!configLoaded"') >= 3, (
            "checkboxes + workspace devem ficar travados até o GET /config "
            "aterrissar (a resposta tardia desfaz edições — corrida A2A-3)"
        )
        assert 'saving || !configLoaded' in content, (
            "o botão Salvar também precisa esperar o config carregar"
        )

    def test_saved_state_updated_on_load_and_save(self):
        content = _content()
        assert content.count("this.savedEnabled = !!this.config.enabled") >= 2, (
            "savedEnabled deve ser atualizado no loadConfig E no saveConfig"
        )
