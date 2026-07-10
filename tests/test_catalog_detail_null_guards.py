"""Guard-rail: expressões Alpine nos MODAIS de catalog_detail.html não podem
derreferenciar estado nulável sem optional chaining.

Achado A2A-1 do E2E "Atlas + A2A" (2026-07-09): os modais (Executar, polling,
Divulgação, recipe...) vivem FORA do `<template x-if="entry">` do conteúdo
principal, então o Alpine avalia suas expressões JÁ NO PAGE LOAD com
`entry: null` (e `currentExecution: null`), e de novo a cada `load()`.
Cada `entry.kind` sem guard virava `Uncaught TypeError` relançado pelo Alpine
(9 por carga da página, mais novos a cada save) — nas baterias E2E isso
coincidiu com o modal "Declarar Divulgação" aparecendo fantasma/quebrado.
O fix (guards `entry?.`) entrou no PR #543; este teste impede regressão.

Regra testada aqui: na região dos modais (do marcador do primeiro modal até o
bloco de scripts), todo acesso é `entry?.prop` / `currentExecution?.prop`.
O padrão `entry.` NÃO casa `entry?.` (o `?` interrompe o literal), então a
região deve ter ZERO ocorrências dos tokens sem guard.

Se este teste falhar: troque `entry.x` por `entry?.x` (idem currentExecution)
na expressão apontada — e cuidado com `:disabled`: a expressão precisa
continuar avaliando para BOOLEANO (undefined deixa o atributo PRESENTE no
Alpine 3 e mata o botão — ver memória/fix do combobox de skills).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAGE = REPO_ROOT / "app" / "templates" / "pages" / "catalog_detail.html"

# Região dos modais: começa no primeiro modal fora do <template x-if="entry">
# e termina onde começa o JS da página (lá os acessos são guardados por fluxo,
# ex.: `if (this.entry) { ... this.entry.kind ... }`).
START_MARKER = "MODAL: editar capability disclosure"
END_MARKER = "{% block scripts %}"

# `(?=[A-Za-z_$])` exige um identificador após o ponto: casa `entry.kind`,
# mas NÃO prosa com reticências ("Selecione a entry...").
UNGUARDED_ENTRY = re.compile(r"\bentry\.(?=[A-Za-z_$])")
UNGUARDED_EXECUTION = re.compile(r"\bcurrentExecution\.(?=[A-Za-z_$])")


def _modal_region() -> tuple[str, int]:
    """Retorna (texto da região dos modais, linha inicial 1-indexed)."""
    content = PAGE.read_text(encoding="utf-8")
    start = content.find(START_MARKER)
    end = content.find(END_MARKER)
    assert start != -1, f"marcador de início não encontrado: {START_MARKER!r}"
    assert end != -1, f"marcador de fim não encontrado: {END_MARKER!r}"
    assert start < end, "região dos modais vazia/invertida — layout mudou?"
    first_line = content[:start].count("\n") + 1
    return content[start:end], first_line


def _hits(pattern: re.Pattern) -> list[str]:
    region, first_line = _modal_region()
    out = []
    for i, line in enumerate(region.splitlines(), start=first_line):
        if pattern.search(line):
            out.append(f"catalog_detail.html:{i}: {line.strip()[:120]}")
    return out


class TestModalNullGuards:
    def test_no_unguarded_entry_in_modals(self):
        hits = _hits(UNGUARDED_ENTRY)
        assert not hits, (
            "acesso a `entry.` sem optional chaining na região dos modais "
            "(entry é null no page load — use `entry?.`):\n" + "\n".join(hits)
        )

    def test_no_unguarded_current_execution_in_modals(self):
        hits = _hits(UNGUARDED_EXECUTION)
        assert not hits, (
            "acesso a `currentExecution.` sem optional chaining na região dos "
            "modais (é null até abrir uma execução — use `currentExecution?.`):\n"
            + "\n".join(hits)
        )
