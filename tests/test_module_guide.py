"""Guarda do Guia dos Módulos (module-guide.js).

- module-guide.js é JS válido e carrega como array em window.MODULE_GUIDE.
- Inclui os módulos novos (Estúdio de Pipelines, Federação, cURL do invoke).
- O contador de módulos no base.html é DINÂMICO (MODULE_GUIDE.length), não um
  número fixo que sai do lugar quando módulos são adicionados.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "app" / "static" / "js" / "module-guide.js"
BASE = ROOT / "app" / "templates" / "layouts" / "base.html"

_NODE = shutil.which("node")


def test_new_modules_present():
    src = MOD.read_text(encoding="utf-8")
    for mid in ("pipeline_studio", "federation", "curl_invoke"):
        assert f"id: '{mid}'" in src, f"módulo novo ausente: {mid}"


def test_module_count_is_dynamic_in_base():
    """O contador não pode ser um número fixo (ex.: '14 módulos')."""
    txt = BASE.read_text(encoding="utf-8")
    assert "window.MODULE_GUIDE ? window.MODULE_GUIDE.length" in txt
    assert not re.search(r"\b14 módulos\b", txt), "contador de módulos ainda fixo em 14"


@pytest.mark.skipif(_NODE is None, reason="node indisponível")
def test_module_guide_loads_and_counts():
    mod = str(MOD).replace("\\", "/")
    harness = (
        'global.window = {};\n'
        f'require("{mod}");\n'
        'const g = global.window.MODULE_GUIDE;\n'
        'if (!Array.isArray(g)) { console.error("não é array"); process.exit(1); }\n'
        'const ids = g.map(m => m.id);\n'
        'for (const need of ["pipeline_studio","federation","curl_invoke"]) {\n'
        '  if (!ids.includes(need)) { console.error("falta "+need); process.exit(1); }\n'
        '}\n'
        'if (g.length < 17) { console.error("esperava >=17 módulos, tem "+g.length); process.exit(1); }\n'
        'console.log("OK " + g.length);\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(harness)
        path = f.name
    try:
        r = subprocess.run([_NODE, path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)
    assert r.returncode == 0, f"module-guide.js falhou:\nSTDOUT={r.stdout}\nSTDERR={r.stderr}"
