"""Guard-rail: nenhum diálogo NATIVO do browser nos templates da UI.

Achado F2 do teste E2E "Órbita" (2026-07-08): `window.confirm()` nativo
bloqueia a página inteira, destoa dos modais do app e quebra automação de
browser (Playwright/Chrome DevTools não conseguem interagir). O caso que
motivou foi o publish() de catalog_detail.html, mas havia 31 ocorrências
em 12 páginas.

A substituição é o helper global `uiConfirm({title, message, confirmLabel,
cancelLabel, danger}) → Promise<boolean>` definido em
app/templates/layouts/base.html (overlay Alpine único, montado no layout).

Se um teste aqui falhar:
- Precisa de confirmação do usuário? Use `await uiConfirm({message: '...',
  danger: true|false})` — NUNCA o confirm nativo. Lembre: o caller precisa
  ser `async` (await em função sync é SyntaxError e derruba o script block
  inteiro do template).
- Precisa de entrada de texto? Faça um modal in-app (ex.: os modais C5 de
  mesh_flow.html), NUNCA `prompt()` nativo.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "app" / "templates"
BASE_HTML = TEMPLATES / "layouts" / "base.html"

# Case-sensitive de propósito: casa `confirm(` e `window.confirm(`,
# mas NÃO casa `uiConfirm(` (o `\b` não separa "ui" de "Confirm" porque
# o C maiúsculo não casa com o `c` literal do padrão).
NATIVE_CONFIRM = re.compile(r"\bconfirm\(")

# `prompt(` exige cuidado com falsos positivos: `system_prompt(`,
# `promptEditorOpen` etc. não podem casar. O lookbehind exclui `\w` e `$`
# imediatamente antes; `window.` opcional cobre a forma qualificada.
NATIVE_PROMPT = re.compile(r"(?<![\w$])(?:window\.)?prompt\(")

# Comentários históricos legítimos mencionam "prompt() nativo" (ex.:
# mesh_flow.html documenta que os modais C5 SUBSTITUÍRAM o prompt nativo).
# Removemos comentários antes de casar — linha inteira ou trailing `//`
# (sem tocar `://` de URLs) e conteúdo de comentário HTML `<!-- ... -->`.
_LINE_COMMENT = re.compile(r"(?<!:)//.*$")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)")


def _strip_comments(line: str) -> str:
    return _LINE_COMMENT.sub("", _HTML_COMMENT.sub("", line))


def _scan(pattern: re.Pattern, *, strip_comments: bool = False) -> list[str]:
    """Retorna ['arquivo:linha: trecho', ...] para cada linha que casa."""
    hits: list[str] = []
    for f in sorted(TEMPLATES.rglob("*.html")):
        rel = f.relative_to(REPO_ROOT).as_posix()
        for i, line in enumerate(
            f.read_text(encoding="utf-8").splitlines(), start=1
        ):
            candidate = _strip_comments(line) if strip_comments else line
            if pattern.search(candidate):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    return hits


class TestNoNativeDialogs:
    def test_zero_native_confirm_in_templates(self):
        """Nenhum template (pages/, layouts/, partials/) pode chamar o
        confirm nativo. Allowlist VAZIA de propósito."""
        hits = _scan(NATIVE_CONFIRM)
        assert not hits, (
            "window.confirm nativo encontrado — use `await uiConfirm({...})` "
            "(helper global de base.html):\n" + "\n".join(hits)
        )

    def test_zero_native_prompt_in_templates(self):
        """Nenhum template pode chamar o prompt nativo (comentários que só
        MENCIONAM o prompt nativo são ignorados)."""
        hits = _scan(NATIVE_PROMPT, strip_comments=True)
        assert not hits, (
            "prompt nativo encontrado — faça um modal in-app "
            "(ver modais C5 de mesh_flow.html):\n" + "\n".join(hits)
        )


class TestUiConfirmHelper:
    def test_base_html_defines_global_uiconfirm(self):
        content = BASE_HTML.read_text(encoding="utf-8")
        assert "window.uiConfirm" in content, (
            "base.html deve registrar o helper global window.uiConfirm"
        )

    def test_base_html_has_modal_testids(self):
        """A automação de browser (motivação do F2) depende destes hooks."""
        content = BASE_HTML.read_text(encoding="utf-8")
        for testid in (
            'data-testid="ui-confirm-modal"',
            'data-testid="ui-confirm-accept"',
            'data-testid="ui-confirm-cancel"',
        ):
            assert testid in content, f"base.html sem {testid}"

    def test_uiconfirm_defaults_ptbr(self):
        """Defaults pt-BR do modal (título e rótulos dos botões)."""
        content = BASE_HTML.read_text(encoding="utf-8")
        assert "Confirmar ação" in content
        assert "'Confirmar'" in content
        assert "'Cancelar'" in content
