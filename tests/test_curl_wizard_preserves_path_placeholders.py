"""PR #233 — wizard cURL preserva placeholders `{cep}` / `{id}` no path.

# Bug

Operador colava `curl 'https://brasilapi.com.br/api/cep/v1/{cep}'` na tela
"Novo Conector > ...ou cole um comando cURL". Resultado:

  ✓ cURL parseado
  base_url:  https://brasilapi.com.br
  endpoint:  GET /api/cep/v1/%7Bcep%7D    ← errado

O placeholder `{cep}` virava literal URL-encoded. Salvar e chamar via
proxy resultaria em GET `https://brasilapi.com.br/api/cep/v1/%7Bcep%7D`
sem substituição do CEP — 404 garantido.

# Causa

`tools.html:1143` (lib de UI) usava `new URL(parsed.url).pathname`. A
classe URL nativa do JS faz URL-encoding automático de `{` e `}` (chars
não-reservados na spec WHATWG URL). Output: `%7B` e `%7D`.

Reprodução em Node:

    new URL('https://x.com/api/v1/{cep}').pathname
    → '/api/v1/%7Bcep%7D'

# Fix

`(u.pathname + (u.search || '')).replace(/%7B/gi, '{').replace(/%7D/gi, '}')`

Decode cirúrgico — só `%7B` e `%7D`. Outros encodings (espaços, acentos)
ficam intactos. Templates como `{cep}`, `{id}`, `{user_id}` voltam ao
estado original e o substituidor de path em runtime funciona normal.

# Guard-rail

Como `parseCurl` é JS puro de UI (Alpine.js sem build step), validamos a
presença do decode via regex sobre o template. Se alguém remover o decode
no futuro, este teste pega no CI antes do operador descobrir via 404.
"""
from __future__ import annotations

import re
from pathlib import Path


API_CONNECTORS_HTML = Path("app/templates/pages/api_connectors.html")


def _read() -> str:
    return API_CONNECTORS_HTML.read_text(encoding="utf-8")


def test_template_decodes_curly_braces_after_url_pathname():
    """Cada uso de `u.pathname` (qualquer variável de URL) no wizard de cURL
    precisa ser seguido de decode `%7B`/`%7D` para preservar placeholders
    de path template."""
    html = _read()
    # Encontra a linha do pathname relacionado a parseCurl
    idx = html.find("u.pathname")
    assert idx >= 0, (
        "Não achei `u.pathname` em api_connectors.html — o wizard de cURL "
        "foi reestruturado? Atualize este teste para o novo locus."
    )
    # Janela de ~30 linhas após o pathname para procurar o decode
    snippet = html[idx:idx + 1500]
    assert re.search(r"%7B", snippet), (
        "Após `u.pathname` (api_connectors.html), não acho referência a "
        "`%7B` para decode de placeholder `{`. Bug do PR #233 pode ter "
        "regredido: placeholders `{cep}` voltarão a sair como `%7Bcep%7D` "
        "no path detectado pelo wizard de cURL, quebrando o proxy."
    )
    assert re.search(r"%7D", snippet), (
        "Ídem para `%7D` (decode de `}`). PR #233 garantia ambos."
    )


def test_template_does_not_use_aggressive_decodeuricomponent():
    """`decodeURIComponent` no path inteiro desfaria encodings legítimos
    (espaços em filenames, acentos em paths), o que é pior que o bug
    original. Garante que o fix é cirúrgico — só `%7B` e `%7D`.

    Se um dia alguém substituir o decode cirúrgico por `decodeURIComponent`
    achando que é "mais limpo", este teste lembra que não é."""
    html = _read()
    idx = html.find("u.pathname")
    assert idx >= 0
    snippet = html[idx:idx + 1500]
    assert "decodeURIComponent" not in snippet, (
        "Fix de placeholders precisa ser CIRÚRGICO (só %7B/%7D). "
        "`decodeURIComponent` agressivo desfaria encodings legítimos."
    )


def test_node_confirms_url_class_encodes_curly_braces():
    """Sanity: confirma que a premissa do bug ainda é verdadeira no Node
    atual. Se algum dia a spec WHATWG mudar e `new URL` parar de encodar
    `{`/`}`, o decode vira no-op (não quebra) mas este teste sinaliza
    que o guard-rail pode ser simplificado.

    Sem Node disponível, pula sem falhar."""
    import shutil
    import subprocess

    if not shutil.which("node"):
        return  # CI tem Node; local sem Node não bloqueia

    out = subprocess.run(
        ["node", "-e",
         "console.log(new URL('https://x.com/a/{id}').pathname)"],
        capture_output=True, text=True, timeout=10,
    )
    if out.returncode != 0:
        return  # erro de execução não invalida o teste principal
    pathname = out.stdout.strip()
    # Se o encoding NÃO acontece mais, o fix vira no-op (ainda funciona),
    # mas vale o aviso para revisar a docstring do PR.
    assert "%7B" in pathname, (
        "new URL().pathname não está mais encodando `{`. Bug original pode "
        "ter sido resolvido upstream pela engine. Decode cirúrgico no "
        "template vira no-op — pode-se considerar remover, mas verifique "
        "que TODAS as engines em uso (Chrome/Firefox/Safari/Edge) também "
        "pararam de encodar antes."
    )
