"""Estação de autenticação reusável dos modais de cURL (curl_auth.js + partial).

Contexto: o snippet de invoke mostrava `X-API-Key: SUA_API_KEY` literal — pra
rodar, o usuário tinha que sair pra Configurações, criar uma chave, copiar o
plaintext (mostrado UMA vez) e voltar pra colar. A "estação" embute a chave no
comando: o modo recomendado **gera a chave agora e injeta** (único instante em
que o plaintext existe, já que o backend guarda só o hash — ver
`app/core/auth_apikey.py`).

Esta suíte trava:
1. Fiação (static analysis dos templates/JS): partial incluído onde deve, factory
   espalhada, símbolos antigos removidos do Fluxograma.
2. Render Jinja real: o `{% include 'partials/curl_auth_modal.html' %}` resolve.
3. Escaping por shell + injeção/máscara da chave (via node) — o ponto crítico de
   segurança: aspas mal escapadas quebram o comando ou vazam o segredo.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "app" / "templates"
PAGES = TPL / "pages"
JS = ROOT / "app" / "static" / "js" / "curl_auth.js"
PARTIAL = TPL / "partials" / "curl_auth_modal.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ───────────────────────── 1. Fiação (static) ─────────────────────────

def test_shared_module_exists_and_exposes_api():
    src = _read(JS)
    for sym in ("buildInvokeCurl", "maskApiKey", "curlAuthStation"):
        assert sym in src, f"curl_auth.js não define {sym}"
    # modo recomendado cria a chave via o endpoint real e usa o nome com origem
    assert "/api/v1/api-keys" in src
    assert "generateAndEmbed" in src
    assert "keyNameHint" in src


def test_base_layout_loads_shared_module():
    assert "/static/js/curl_auth.js" in _read(TPL / "layouts" / "base.html")


def test_partial_has_three_modes_and_guardrails():
    src = _read(PARTIAL)
    assert "curlAuth.open" in src
    # 3 modos
    assert "curlAuth.mode='embed'" in src
    assert "curlAuth.mode='existing'" in src
    assert "curlAuth.mode='placeholder'" in src
    # botão de gerar + aviso de única-vez + máscara/reveal + cópia
    assert "generateAndEmbed()" in src
    assert "só agora" in src              # banner de segredo efêmero
    assert "curlAuth.reveal" in src       # toggle de máscara
    assert "copyCurlAuth()" in src


def test_mesh_flow_uses_station_and_drops_legacy_symbols():
    src = _read(PAGES / "mesh_flow.html")
    assert "{% include 'partials/curl_auth_modal.html' %}" in src
    assert "...curlAuthStation()" in src
    assert "openPipelineCurl()" in src
    assert "/api/v1/pipelines/' + this.selectedPipeline.id + '/invoke'" in src
    # símbolos do modal antigo NÃO podem sobrar (viraram a estação compartilhada)
    for legacy in ("curlModal", "copyCurl(", "get curlCommand", "openCurlModal"):
        assert legacy not in src, f"símbolo legado {legacy!r} ainda presente em mesh_flow.html"


def test_settings_reveal_uses_shared_builder():
    src = _read(PAGES / "settings.html")
    assert "justCreatedCurl()" in src
    assert "buildInvokeCurl" in src
    assert "CURL_AUTH_SHELLS" in src      # tabs de shell no exemplo da chave nova


def test_catalog_detail_wires_integration_curl():
    src = _read(PAGES / "catalog_detail.html")
    assert "{% include 'partials/curl_auth_modal.html' %}" in src
    assert "...curlAuthStation()" in src
    assert "openCatalogCurl()" in src
    assert "execute-pipeline" in src
    assert "bodyKey: 'input'" in src       # catálogo usa {"input":...}, não {"message":...}


# ───────────────────────── 2. Render Jinja real ─────────────────────────

@pytest.mark.parametrize("page", ["pages/mesh_flow.html", "pages/catalog_detail.html"])
def test_partial_include_resolves_at_render(page):
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TPL)),
        undefined=jinja2.ChainableUndefined,
        autoescape=False,
    )
    html = env.get_template(page).render(
        app_version="x", user_role="admin", entry_id="demo",
        request={}, user={"id": "u", "role": "admin"},
    )
    assert "curlAuth.open" in html, f"{page}: partial da estação não foi incluído no render"


# ───────────────────────── 3. Escaping + chave (node) ─────────────────────────

_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="node não disponível pra checar o builder JS")
def test_builder_escaping_and_key_injection_via_node():
    """O coração de segurança: escaping por shell + injeção/máscara da chave.
    Roda o módulo real sob node e asserta os invariantes."""
    mod = str(JS).replace("\\", "/")
    # Raw string: o que está escrito aqui é EXATAMENTE o fonte JS (sem reprocesso
    # de escapes do Python) — crucial pra acertar a contagem de barras por shell.
    harness = r'''
require("__MOD__");
const b = globalThis.buildInvokeCurl, mask = globalThis.maskApiKey;
const KEY = 'ag_live_0zb-1HH1ub4pHe4l5HxSckY-wHOpYbUU';
function assert(c, m){ if(!c){ console.error('FAIL: '+m); process.exit(1); } }

// chave injetada no header em todos os shells
for (const sh of ['bash','powershell','cmd']) {
    assert(b({url:'u', message:'oi', shell:sh, apiKey:KEY}).includes(KEY), 'chave ausente em '+sh);
}
// sem chave -> placeholder
assert(b({url:'u', message:'oi', shell:'bash'}).includes('SUA_API_KEY'), 'placeholder ausente');

// bash: aspas simples na mensagem viram '\'' (nao quebram o -d '...')
assert(b({url:'u', message:"it's", shell:'bash'}).includes("it'\\''s"), 'bash quote escaping');
// powershell: aspas simples duplicadas ('')
assert(b({url:'u', message:"it's", shell:'powershell'}).includes("it''s"), 'ps quote escaping');
// cmd: aspas duplas do JSON escapadas (cada " do JSON \" vira \\")
assert(b({url:'u', message:'say "hi"', shell:'cmd'}).includes('\\\\"hi\\\\"'), 'cmd quote escaping');

// bodyKey alterna a chave do payload
assert(b({url:'u', message:'oi', shell:'bash', bodyKey:'input'}).includes('"input":"oi"'), 'bodyKey input');
assert(b({url:'u', message:'oi', shell:'bash'}).includes('"message":"oi"'), 'bodyKey default message');

// mascara: mantem prefixo (12) + ultimos 4, esconde o miolo
const m = mask(KEY);
assert(m.startsWith('ag_live_0zb-'), 'mascara perdeu o prefixo');
assert(m.endsWith('YbUU'), 'mascara perdeu os ultimos 4');
assert(!m.includes('1HH1ub4pHe4l5HxSckY'), 'mascara vazou o miolo');
assert(mask('curta') === 'curta', 'chave curta deveria ficar intacta');

console.log('OK');
'''.replace("__MOD__", mod)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, f"builder JS falhou:\nSTDOUT={r.stdout}\nSTDERR={r.stderr}"
    assert "OK" in r.stdout
