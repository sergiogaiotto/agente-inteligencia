"""Regressão para PR #228 — card de stats da KB ficava stale após mutações
de tabela (promote-to-table / data-tables append).

# Bug original

Sintoma: usuário promove 2 abas distintas de um XLSX em sequência. Painel
direito (aba "Tabelas") mostra 2 tabelas corretamente, mas o **card
esquerdo** da KB mostra "1 tabela · 48 linhas" — só a primeira contagem.
Recarregar a página resolve, mas é experiência ruim.

# Causa

O caller frontend de `/promote-to-table` (e `/data-tables/{id}/append`)
chamava `loadTablesForSource(sid)` para atualizar a lista do inspector,
mas **não** invalidava `statsBy[sid]` (que alimenta o card). Race entre
duas fontes de dados sobre a mesma KB.

# Fix (e proteção)

PR #228 adicionou `refreshStatsFor(sid)` logo após `loadTablesForSource`
em **todos os callers que mutam data_tables da KB**. Este teste varre o
template para garantir que esse invariante não regrida.

Estratégia: para cada chamada de `loadTablesForSource` em contexto de
*sucesso de mutação* (depois de `await fetch(...promote/append...)`),
exige uma chamada a `refreshStatsFor` dentro de uma janela curta (≤ 8
linhas). Janela pequena evita falso-positivo em handlers que só LEEM
(ex: `openInspect`).
"""
from __future__ import annotations

import re
from pathlib import Path


EVIDENCE_HTML = Path("app/templates/pages/evidence.html")


def _read_template() -> str:
    return EVIDENCE_HTML.read_text(encoding="utf-8")


# Marcadores de endpoint dentro do JS Alpine. URLs são montadas por
# concatenação (`'/api/v1/...' + id + '/promote-to-table'`), então buscamos
# o sufixo final como string literal — mais robusto que tentar regex sobre
# a expressão completa.
_MUTATION_ENDPOINTS = [
    "/promote-to-table",
    "/append",  # POST /api/v1/data-tables/{id}/append
]


def _find_mutation_blocks(html: str) -> list[tuple[str, int]]:
    """Retorna [(endpoint, line_no)] de cada literal de endpoint de mutação."""
    blocks: list[tuple[str, int]] = []
    for ep in _MUTATION_ENDPOINTS:
        # Procura o sufixo literal em quote single ou double
        for m in re.finditer(
            rf"['\"]{re.escape(ep)}['\"]",
            html,
        ):
            line_no = html[: m.start()].count("\n") + 1
            blocks.append((ep, line_no))
    return blocks


def test_each_promote_or_append_invalidates_stats():
    """Para cada chamada de promote-to-table / append no JS Alpine, o handler
    precisa invalidar stats da KB via refreshStatsFor logo após
    loadTablesForSource. Sem isso o card esquerdo fica stale (PR #228).
    """
    html = _read_template()
    lines = html.split("\n")
    blocks = _find_mutation_blocks(html)
    assert blocks, (
        "Sanity: nenhum POST de promote-to-table/append encontrado em "
        f"{EVIDENCE_HTML}. Test virou letra morta se UI mudou — atualizar."
    )

    problems: list[str] = []
    for endpoint, line_no in blocks:
        # Janela de 50 linhas após o fetch (o handler todo cabe nisso)
        window = "\n".join(lines[line_no - 1 : line_no - 1 + 50])

        if "loadTablesForSource" not in window:
            # Esse fetch pode estar em um handler que não lista tabelas
            # (ex: stub); só checamos o pareamento quando o "ancora" aparece.
            continue

        if "refreshStatsFor" not in window:
            problems.append(
                f"  endpoint='{endpoint}' linha {line_no}: chama "
                f"loadTablesForSource mas não chama refreshStatsFor na "
                f"janela seguinte. Card de stats vai ficar stale."
            )

    assert not problems, (
        "Handler(s) de mutação de data_tables esquecem de invalidar stats "
        "do card de KB. Adicione `if (this.refreshStatsFor) await "
        f"this.refreshStatsFor(sid);` após loadTablesForSource.\n\n"
        + "\n".join(problems)
    )


def test_helper_refreshstatsfor_existe_no_template():
    """Sanity: a função `refreshStatsFor` precisa estar definida no Alpine
    page para os callers acima funcionarem. Protege contra rename/exclusão."""
    html = _read_template()
    # Definição (declaração de método: `async refreshStatsFor(...`)
    assert re.search(r"\basync\s+refreshStatsFor\s*\(", html), (
        f"Método refreshStatsFor ausente em {EVIDENCE_HTML}. "
        "Foi renomeado? Atualize também os callers neste mesmo template."
    )
