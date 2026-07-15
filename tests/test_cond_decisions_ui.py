"""Contrato de Decisão — card de UI no editor de conexão (Cond-C, 35.19.0).

Varredura de template (padrão do projeto — ver test_cond_else_phrases): garante
a presença das peças; o comportamento Alpine em runtime é verificado no smoke
de browser real (lição do repo: grep de template NÃO pega quebra de runtime).
"""
from pathlib import Path

SRC = (Path(__file__).parent.parent / "app" / "templates" / "pages" / "mesh_flow.html").read_text(encoding="utf-8")


def test_load_src_decisions_definido_e_chamado():
    # o critical do review 2026-07-15: os call sites existiam SEM a definição —
    # TypeError quebrava criar/editar QUALQUER conexão.
    assert "async loadSrcDecisions(srcId)" in SRC
    assert SRC.count("this.loadSrcDecisions(") >= 2  # openNewEditor + editEdge


def test_guarda_de_epoca_do_contrato():
    # resposta atrasada de um editor anterior não pode contaminar o editor
    # atual com contrato de OUTRO agente (mesma classe do _phrasesGen).
    assert "this.editor.source === srcId" in SRC


def test_optgroup_do_contrato_no_card_decisao():
    assert "Contrato do agente (## Decisions)" in SRC
    assert "srcDecisionOptions()" in SRC
    # cada valor do contrato vira fragmento de expr pronto
    assert "'decision.' + f + ' == ' + this._jinjaStrExact(v)" in SRC


def test_source_agent_id_nas_tres_chamadas():
    # simulador + Frases-Prova espelham o runtime; tradutor conhece o contrato.
    # (o `source_agent_id: ed.source` SEM fallback é o body do save, pré-existente)
    assert SRC.count("source_agent_id: ed.source || ''") == 2  # simulate + runPhrases
    assert "source_agent_id: this.editor.source" in SRC        # suggest-conditional


def test_draft_label_deriva_rotulo_do_contrato():
    # sem o fallback, addClause gravava cláusula com rótulo em branco
    assert "/^decision\\.([A-Za-z_]\\w*)\\s*==\\s*'(.*)'$/" in SRC
