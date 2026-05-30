"""Footer do binding form: explica melhor o que significa "sem LLM".

User reportou (2026-05-30) confusão com a frase antiga "Envio direto
sem LLM — payload monta a partir destes campos" porque, no contexto de
RAG, embeddings ainda são usados pra busca vetorial. A frase dava a
impressão de que NENHUM modelo era usado, o que não é verdade.

Fix: "Sem LLM intermediário — payload exato vai pro motor"
- "intermediário" deixa claro que é o LLM de decisão/orquestração
  (não o embedding model do RAG)
- "payload exato vai pro motor" reforça o benefício real:
  determinismo, sem alucinação, zero tokens LLM
- Tooltip explica em detalhes pra quem hover
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def html():
    return Path("app/templates/pages/workspace.html").read_text(encoding="utf-8")


class TestFooterText:
    def test_new_wording_present(self, html):
        """Texto novo do footer (curto, sem mentir sobre embeddings)."""
        assert "Sem LLM intermediário — payload exato vai pro motor" in html

    def test_old_misleading_wording_removed(self, html):
        """A frase antiga dava a impressão de que ZERO modelos eram usados —
        confundia user no contexto RAG. Removida."""
        assert "Envio direto sem LLM — payload monta a partir destes campos" not in html

    def test_tooltip_explains_embedding_caveat(self, html):
        """title= no span tem explicação detalhada pro user que hover
        entender que embeddings (determinísticos) ainda rodam — só o LLM
        de DECISÃO foi bypassado."""
        assert "Sem LLM de decisão/orquestração" in html
        assert "busca vetorial" in html
        assert "determinístico" in html
