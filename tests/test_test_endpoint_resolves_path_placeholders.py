"""PR #236 — botão Testar substitui placeholders {x} no path antes de chamar
o proxy.

# Bug original

Operador editou endpoint `GET /api/cep/v1/{cep}` (Brasilapi) e clicou em
"Testar". Resposta: HTTP 400 do upstream com mensagem:

  {"message":"CEP deve conter exatamente 8 caracteres",
   "type":"validation_error",
   "errors":[{"message":"CEP informado possui menos do que 8 caracteres",
              "service":"cep_validation"}]}

A UI enviava `this.epForm.path` direto para o proxy, sem resolver `{cep}`.
Brasilapi recebia literalmente `/api/cep/v1/{cep}` → o servidor lia
`{cep}` (5 chars) como o valor do path-parameter `cep` → falhava
validação interna.

# Fix

UI detecta placeholders `{x}` no path com `/\{([^}]+)\}/g`. Para cada
um, mostra um input inline acima do botão Testar. Operador preenche
valores reais antes de testar. Helper `placeholderSuggestion(name)`
sugere valores comuns no campo placeholder do input (ex: 01310100 para
cep, 11 para ddd, 33000167000101 para cnpj).

No clique do Testar, substitui `{cep}` por `encodeURIComponent(valor)`
no path resolvido. URL final fica `/api/cep/v1/01310100`.

# Guard-rail

Como toda a lógica é JS de UI (Alpine sem build), os testes validam
estaticamente o template — regex sobre o source. Se alguém remover o
resolver ou trocar pelo path original (regressão), CI quebra antes do
operador descobrir via HTTP 400.

Reproduz a chamada do navegador via Node para o regex de extração de
placeholders.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


HTML = Path("app/templates/pages/api_connectors.html")


def _read() -> str:
    return HTML.read_text(encoding="utf-8")


# ─── 1. Template tem getter + bloco de inputs ───────────────


class TestTemplateHasResolver:
    def test_getter_extracting_placeholders_exists(self):
        """`epPathPlaceholders` é o getter que extrai `{x}` únicos do path."""
        html = _read()
        assert "get epPathPlaceholders" in html, (
            "Getter `epPathPlaceholders` removido. Sem ele a UI não sabe "
            "quais placeholders existem e o Testar volta a falhar com 400."
        )

    def test_test_endpoint_uses_resolved_path_not_form_path(self):
        """No corpo de `testEndpoint`, o POST /proxy deve mandar `path: resolvedPath`,
        não `path: this.epForm.path`."""
        html = _read()
        # Encontra o trecho da chamada do proxy dentro do testEndpoint
        idx = html.find("async testEndpoint()")
        assert idx >= 0, "função testEndpoint sumiu"
        body = html[idx:idx + 3500]
        assert "resolvedPath" in body, (
            "testEndpoint não usa variável resolvedPath — placeholders não "
            "estão sendo substituídos antes do POST /proxy. Regredido para "
            "o bug original."
        )
        # E que o body do POST usa essa variável (não o this.epForm.path direto)
        assert re.search(r"path:\s*resolvedPath", body), (
            "POST /proxy não passa `path: resolvedPath`. Operador vai ver HTTP 400."
        )

    def test_template_renders_inline_inputs_for_each_placeholder(self):
        """Para cada placeholder do path, a UI deve mostrar um input para o
        operador preencher antes de testar."""
        html = _read()
        assert "epTestPathValues" in html, (
            "Estado `epTestPathValues` removido — operador não tem onde "
            "digitar o valor real do placeholder."
        )
        assert "Path tem placeholder" in html, (
            "Banner amarelo que alerta o operador sobre placeholders sumiu — "
            "ele não vai entender por que precisa preencher esses campos."
        )

    def test_placeholder_suggestion_covers_common_brazilian_apis(self):
        """Helper `placeholderSuggestion` sugere valores para os placeholders
        mais comuns de APIs públicas brasileiras (Brasilapi etc.). Sem isso,
        operador leigo ficava sem saber o que digitar."""
        html = _read()
        assert "placeholderSuggestion" in html
        # Pelo menos cep + cnpj + ddd presentes (cobre Brasilapi)
        idx = html.find("placeholderSuggestion(name)")
        assert idx >= 0
        snippet = html[idx:idx + 2000]
        for key in ("cep", "cnpj", "ddd", "id"):
            assert f"{key}:" in snippet, (
                f"placeholderSuggestion não cobre '{key}'. Operador leigo "
                f"abriria endpoint com {{{key}}} sem saber o que preencher."
            )


# ─── 2. Reset entre endpoints ────────────────────────────────


class TestStateResetWhenOpeningNewEndpoint:
    def test_open_endpoint_form_resets_path_values(self):
        """openEndpointForm deve zerar epTestPathValues — sem isso, abrir
        endpoint A com `{cep}=01310100`, depois endpoint B com `{cep}` na
        mesma KB, o input já estaria preenchido com o valor antigo."""
        html = _read()
        idx = html.find("openEndpointForm(connId, ep)")
        assert idx >= 0
        body = html[idx:idx + 1500]
        assert "epTestPathValues = {}" in body, (
            "openEndpointForm não reseta epTestPathValues — valores vazam "
            "entre endpoints, confundindo o operador."
        )


# ─── 3. Sanity: regex de extração funciona no Node ───────────


def test_node_extracts_placeholders_with_same_regex():
    """Sanity opcional: o regex `/\\{([^}]+)\\}/g` que o template usa
    realmente extrai placeholders corretamente. Skip se Node ausente."""
    if not shutil.which("node"):
        return

    out = subprocess.run(
        ["node", "-e",
         "const m='/api/cep/v1/{cep}/details/{format}'.match(/\\{([^}]+)\\}/g);"
         "console.log(JSON.stringify(m));"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0:
        return
    matches = out.stdout.strip()
    assert matches == '["{cep}","{format}"]', (
        f"Regex não extrai placeholders consistentemente. Output: {matches!r}"
    )
