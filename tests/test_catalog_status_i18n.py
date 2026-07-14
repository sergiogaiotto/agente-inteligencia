"""i18n dos status do Catálogo + termo "pré-checks" → pt-BR.

Garante que:
1. O helper compartilhado catalog_status.js mapeia os status de lifecycle e de
   revisão para pt-BR (display-only; os value= dos <option> seguem em inglês).
2. Os filtros de status (catalog.html, catalog_inventory.html) mostram texto
   pt-BR mas mantêm value="draft" etc. (senão o filtro server-side quebra).
3. O termo "pré-checks" foi padronizado para "pré-verificações" nas telas.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "app" / "templates" / "pages"
JS = ROOT / "app" / "static" / "js" / "catalog_status.js"

_NODE = shutil.which("node")

# value= que DEVEM continuar em inglês (alinhado ao DB/API)
_LIFECYCLE = ["draft", "submitted", "approved", "published", "deprecated", "archived"]
# texto visível pt-BR esperado por status
_PTBR = {
    "draft": "Rascunho", "submitted": "Em revisão", "approved": "Aprovada",
    "published": "Publicada", "deprecated": "Depreciada", "archived": "Arquivada",
}


def _read(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("page", ["catalog.html", "catalog_inventory.html"])
def test_status_filter_value_english_text_ptbr(page):
    """<option value="draft">Rascunho</option>: value inglês, texto pt-BR."""
    html = _read(page)
    for status in _LIFECYCLE:
        assert f'<option value="{status}">{_PTBR[status]}</option>' in html, (
            f"{page}: filtro de status para '{status}' deveria ser "
            f'<option value="{status}">{_PTBR[status]}</option>'
        )
    # não pode sobrar o texto cru em inglês nos <option> de status
    for raw in ("Draft", "Submitted", "Approved", "Published", "Deprecated", "Archived"):
        assert f'>{raw}</option>' not in html, f"{page}: <option> ainda em inglês: {raw}"


def test_status_badges_use_helper():
    """Badges de status do catálogo/queue/inventário usam catalogStatusLabel()."""
    assert "catalogStatusLabel(entry.status)" in _read("catalog.html")
    assert "catalogStatusLabel(item.entry?.status)" in _read("catalog_queue.html")
    assert "catalogStatusLabel(" in _read("catalog_inventory.html")


def test_prechecks_term_standardized():
    """'pré-checks' virou 'pré-verificações' nas telas visíveis."""
    queue = _read("catalog_queue.html")
    assert "pré-verifica" in queue
    assert "pré-checks" not in queue.lower()
    # detalhe: histórico de submissões
    assert "pré-checks" not in _read("catalog_detail.html").lower()


@pytest.mark.skipif(_NODE is None, reason="node indisponível pra checar o helper JS")
def test_status_helper_maps_ptbr_via_node():
    mod = str(JS).replace("\\", "/")
    harness = r'''
require("__MOD__");
const sl = globalThis.catalogStatusLabel, rl = globalThis.catalogReviewLabel;
function assert(c, m){ if(!c){ console.error('FAIL: '+m); process.exit(1); } }
assert(sl('draft') === 'Rascunho', 'draft');
assert(sl('submitted') === 'Em revisão', 'submitted');
assert(sl('published') === 'Publicada', 'published');
assert(sl('deprecated') === 'Depreciada', 'deprecated');
assert(sl('desconhecido') === 'desconhecido', 'fallback devolve o valor cru');
assert(rl('pending') === 'Pendente', 'review pending');
assert(rl('changes_requested') === 'Mudanças solicitadas', 'review changes_requested');
console.log('OK');
'''.replace("__MOD__", mod)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, f"helper JS falhou:\nSTDOUT={r.stdout}\nSTDERR={r.stderr}"
