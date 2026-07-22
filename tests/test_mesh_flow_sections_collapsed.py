"""67.1.0 — Grupos do trilho de pipelines RECOLHIDOS na entrada do Fluxo de agentes.

Pedido do dono do produto: com dezenas de pipelines, a lista aberta empurrava
"Criar a partir de um fluxo" para fora da dobra. Regras seladas:
- rascunho/publicado/aposentado começam FECHADOS;
- members/include/simulate (seções do PAINEL do pipeline selecionado) seguem
  ABERTAS — recolher aqui esconderia o conteúdo principal do painel;
- seleção PROGRAMÁTICA (ex.: pipeline recém-criado via "Novo" chama
  selectPipeline(fresh)) abre o grupo do status — senão o card selecionado
  nasceria invisível no trilho recolhido.

Testes de template leem o ARQUIVO CRU (padrão da casa — pytest não executa JS).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = (REPO_ROOT / "app" / "templates" / "pages" / "mesh_flow.html").read_text(encoding="utf-8")


def _section_open_literal() -> str:
    m = re.search(r"sectionOpen:\s*\{[^}]*\}", SRC)
    assert m, "literal sectionOpen não encontrado"
    return m.group(0)


def test_grupos_do_trilho_comecam_recolhidos():
    lit = _section_open_literal()
    assert "rascunho: false" in lit
    assert "publicado: false" in lit
    assert "aposentado: false" in lit


def test_secoes_do_painel_continuam_abertas():
    # members/include/simulate são do PAINEL do pipeline, não do trilho —
    # recolhê-las junto esconderia o conteúdo principal ao selecionar.
    lit = _section_open_literal()
    assert "members: true" in lit
    assert "include: true" in lit
    assert "simulate: true" in lit


def test_selecao_programatica_abre_o_grupo_do_status():
    # selectPipeline(fresh) roda após criar pipeline via "Novo" — o grupo do
    # status precisa abrir, senão o card selecionado fica invisível.
    i = SRC.index("selectPipeline(p) {")
    body = SRC[i:SRC.index("},", i)]
    assert "this.sectionOpen[p.status] = true" in body
