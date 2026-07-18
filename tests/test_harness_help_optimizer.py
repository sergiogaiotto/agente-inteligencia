"""Ajuda desta página do Harness — Otimização automática (item 3, 52.1.0).

O drawer de ajuda da tela de Avaliação ganhou profundidade sobre a
Otimização automática: fundamentos (DSPy/GEPA, report-only, anti-Goodhart),
conceitos (split, McNemar, Pareto, sonda, teto, selo), caso de uso narrado
em 4 passos e exemplos COM NÚMEROS — que precisam estar estatisticamente
CORRETOS (o exemplo ensina o operador a ler o p-valor).
"""
from __future__ import annotations

import re
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELP = ROOT / "app" / "static" / "js" / "help-content.js"


def _harness_entry() -> str:
    src = HELP.read_text(encoding="utf-8")
    m = re.search(r"\n  harness: \{.*?\n  \},\n", src, re.S)
    assert m, "entry harness ausente no help-content.js"
    return m.group(0)


def test_summary_mentions_optimizer():
    blk = _harness_entry()
    m = re.search(r"summary: '([^']+)'", blk)
    assert m and "Otimização automática" in m.group(1), (
        "summary do harness não menciona a Otimização automática"
    )


def test_optimizer_fundamentals_cover_core_concepts():
    blk = _harness_entry()
    for concept in ("DSPy", "GEPA", "report-only", "treino/holdout",
                    "Champion", "challenger", "McNemar", "discordantes",
                    "grounded", "anti-Goodhart", "go/no-go",
                    "Pareto por caso", "Teto de custo", "Model Drifting"):
        assert concept.lower() in blk.lower(), (
            f"fundamentos da otimização sem o conceito '{concept}'"
        )


def test_optimizer_walkthrough_has_four_steps():
    blk = _harness_entry()
    m = re.search(r"title: 'Otimização — na prática'.*?\]", blk, re.S)
    assert m, "aba 'Otimização — na prática' ausente"
    steps = m.group(0)
    for marker in ("0. Preparar", "1. Caminho manual", "2. Caminho automático",
                   "3. Depois de promover"):
        assert marker in steps, f"passo '{marker}' ausente do caso de uso"
    assert "optimizer_loop_enabled" in steps, "flag do loop não documentada"
    assert "Dividir treino/holdout" in steps


def test_optimizer_examples_and_limits():
    blk = _harness_entry()
    m = re.search(r"title: 'Otimização — exemplos e limites'.*?\]", blk, re.S)
    assert m, "aba de exemplos/limites ausente"
    ex = m.group(0)
    assert "improved=false" in ex, "anti-overfit (holdout empate) sem exemplo"
    assert "revisão" in ex.lower(), "revisão restaurável não mencionada"
    assert "4+ casos" in ex, "mínimo de treino não documentado"


def test_example_pvalues_are_statistically_correct():
    """Os números do exemplo ENSINAM a ler o McNemar — se estiverem errados,
    o operador aprende estatística errada. Recalcula o p exato bilateral
    (binomial p=0.5 sobre os discordantes) para cada padrão citado."""
    def mcnemar_p(b: int, c: int) -> float:
        n = b + c
        if n == 0:
            return 1.0
        k = max(b, c)
        tail = sum(comb(n, i) for i in range(k, n + 1)) / 2 ** n
        return min(1.0, 2 * tail)

    blk = _harness_entry()
    # padrões citados no texto: 6×0 significativo; 8×1 significativo;
    # 6×1 NÃO; 1×0 inconclusivo
    assert mcnemar_p(6, 0) < 0.05 and "6×0" in blk
    assert mcnemar_p(8, 1) < 0.05 and "8×1" in blk
    assert mcnemar_p(6, 1) > 0.05 and "6×1" in blk
    assert mcnemar_p(1, 0) == 1.0 and "1×0" in blk
    assert "p≈0.031" in blk and "p=0.125" in blk and "p=1.0" in blk


def test_fundamentals_mention_new_panel_features():
    """A tela mudou (filtros 51.0.0 + CSV 52.0.0) — a ajuda descreve."""
    blk = _harness_entry()
    assert "filtros" in blk.lower(), "filtros dos painéis não documentados"
    assert "importar CSV" in blk or "exportar CSV" in blk or \
           "template/exportar/importar CSV" in blk, "CSV não documentado"
    assert "célula vazia mantém" in blk.lower(), (
        "semântica parcial do modo atualizar não documentada"
    )
    assert "split" in blk and "holdout" in blk


def test_modal_follow_documented():
    blk = _harness_entry()
    assert "Acompanhar" in blk, "modal de acompanhamento não citado"
    assert "não cancela o job" in blk or "segue no servidor" in blk, (
        "ajuda não explica que fechar o modal não cancela o loop"
    )


def test_static_scripts_carry_version_cache_buster():
    """Achado ao verificar ESTE conteúdo ao vivo: sem ?v= o browser serve o
    help-content.js do cache e atualização de ajuda NUNCA chega ao usuário.
    Amarra o cache ao APP_VERSION (todo PR bumpa → todo deploy invalida)."""
    base = (ROOT / "app" / "templates" / "layouts" / "base.html"
            ).read_text(encoding="utf-8")
    for js in ("module-guide.js", "help-content.js", "curl_auth.js",
               "catalog_status.js"):
        assert f'/static/js/{js}?v={{{{ app_version }}}}' in base, (
            f"{js} sem cache-busting — conteúdo novo não chega ao browser"
        )
