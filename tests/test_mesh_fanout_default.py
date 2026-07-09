"""F5 — fan-out condicional SEM rota default·else.

O selo ⚠ fan-out (isFanoutRoot) só conta ≥2 condicionais; não distingue se há
uma aresta ``default`` de fallback. Sem ela, uma saída do roteador que não casa
com nenhuma ``expr`` vira dead-end. `_fanout_missing_default` detecta o padrão e
`get_topology` o expõe; o Fluxograma avisa no painel do nó.
"""

from __future__ import annotations

from pathlib import Path

from app.routes.mesh import _fanout_missing_default


def _cond(id_, src, tgt, expr="x"):
    return {"id": id_, "source": src, "target": tgt, "type": "conditional",
            "config": f'{{"expr":"{expr}"}}'}


def test_flags_fanout_without_default():
    edges = [_cond("e1", "R", "A"), _cond("e2", "R", "B")]
    assert _fanout_missing_default(edges) == ["R"]


def test_not_flagged_when_default_edge_present():
    edges = [
        _cond("e1", "R", "A"), _cond("e2", "R", "B"),
        {"id": "e3", "source": "R", "target": "C", "type": "default", "config": "{}"},
    ]
    assert _fanout_missing_default(edges) == []


def test_single_conditional_is_not_fanout():
    assert _fanout_missing_default([_cond("e1", "R", "A")]) == []


def test_flow_editor_wires_the_warning():
    html = (
        Path(__file__).resolve().parent.parent
        / "app" / "templates" / "pages" / "mesh_flow.html"
    ).read_text(encoding="utf-8")
    assert "isFanoutMissingDefault" in html, "getter ausente no editor de fluxo"
    assert "fanout_missing_default" in html, "sinal do /topology não consumido"
    assert "default·else" in html, "aviso de rota default·else ausente"
