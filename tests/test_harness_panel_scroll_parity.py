"""Paridade de altura entre os painéis Golden Dataset × Execuções (49.1.0).

Achado de UX (screenshot do usuário, 2026-07-17): a lista do Gold travava em
320px (max-h-80) enquanto a de Execuções usava 55vh — a linha do grid ficava
desbalanceada e o scrollbar do Gold desproporcional. Os DOIS painéis agora
compartilham o MESMO teto de altura (55vh) com scroll interno próprio.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "app" / "templates" / "pages" / "harness.html"


def _src() -> str:
    return HARNESS.read_text(encoding="utf-8")


def _scroll_classes(testid: str) -> str:
    """Classes do container de scroll identificado pelo data-testid."""
    src = _src()
    m = re.search(
        r'<div class="([^"]*)"[^>]*data-testid="' + re.escape(testid) + '"',
        src,
    )
    assert m, f"container data-testid={testid} ausente no harness.html"
    return m.group(1)


def test_both_lists_share_same_max_height():
    gold = _scroll_classes("gold-list-scroll")
    runs = _scroll_classes("runs-list-scroll")
    gold_h = re.search(r"max-h-\[?[\w%.]+\]?", gold)
    runs_h = re.search(r"max-h-\[?[\w%.]+\]?", runs)
    assert gold_h and runs_h, "um dos painéis perdeu o teto de altura"
    assert gold_h.group(0) == runs_h.group(0), (
        f"alturas divergem: gold={gold_h.group(0)} runs={runs_h.group(0)} — "
        "os painéis lado a lado precisam do MESMO teto (regressão do fix 49.1.0)"
    )


def test_both_lists_scroll_internally():
    for testid in ("gold-list-scroll", "runs-list-scroll"):
        cls = _scroll_classes(testid)
        assert "overflow-y-auto" in cls, f"{testid} sem scroll interno"
        assert "scrollbar-thin" in cls, f"{testid} sem scrollbar-thin"


def test_gold_list_not_pinned_to_320px():
    """O bug original: max-h-80 (320px) fixo no Gold."""
    gold = _scroll_classes("gold-list-scroll")
    assert "max-h-80" not in gold.split(), "Gold voltou ao teto de 320px"
