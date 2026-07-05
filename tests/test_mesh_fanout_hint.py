"""Detecção de fan-out p/ o hint educativo do Compor/Conexões (Fatia 3b).

Quando o operador cabeia 2+ destinos como irmãos `conditional` sob o MESMO
roteador (fan-out 1-de-N), o roteador escolhe UM destino — os outros NÃO recebem
o resultado do escolhido. Se a intenção era uma CADEIA (um destino consome o
output de outro, ex.: Tavily busca pontos turísticos A PARTIR do endereço que o
Busca endereço resolveu), o fan-out é o cabeamento errado.

`_fanout_roots` identifica essas origens (≥2 arestas conditional de saída) para a
UI mostrar um aviso fan-out × cadeia no momento da cabeação. É genérico e sem
falso-positivo (não tenta adivinhar a dependência semântica — só sinaliza o
padrão e educa). Ver mesh.html (banner) e get_topology (expõe `fanout_roots`).
"""
from __future__ import annotations

from app.routes.mesh import _fanout_roots, _conditional_without_expr


def _edge(src: str, tgt: str, type: str = "conditional") -> dict:
    return {"id": f"{src}->{tgt}", "source": src, "target": tgt, "type": type}


def test_two_conditional_siblings_flagged():
    assert _fanout_roots([_edge("R", "A"), _edge("R", "B")]) == ["R"]


def test_three_conditional_siblings_flagged_once():
    roots = _fanout_roots([_edge("R", "A"), _edge("R", "B"), _edge("R", "C")])
    assert roots == ["R"]


def test_single_conditional_not_flagged():
    assert _fanout_roots([_edge("R", "A")]) == []


def test_conditionals_from_different_sources_not_flagged():
    # cada origem tem só 1 conditional → nenhum fan-out
    assert _fanout_roots([_edge("R", "A"), _edge("S", "B")]) == []


def test_only_conditional_counts_not_sequential():
    # 1 conditional + 1 sequential sob a mesma origem → não é fan-out 1-de-N
    edges = [_edge("R", "A", "conditional"), _edge("R", "B", "sequential")]
    assert _fanout_roots(edges) == []


def test_default_edge_does_not_count():
    # conditional + default (else): é exatamente a cadeia CERTA (1 ramo + else),
    # não um fan-out de 2 condicionais → não avisa.
    edges = [_edge("R", "A", "conditional"), _edge("R", "B", "default")]
    assert _fanout_roots(edges) == []


def test_multiple_fanout_roots():
    edges = [
        _edge("R", "A"), _edge("R", "B"),   # R é fan-out
        _edge("S", "C"), _edge("S", "D"),   # S é fan-out
        _edge("T", "E"),                     # T não
    ]
    assert sorted(_fanout_roots(edges)) == ["R", "S"]


# ── _conditional_without_expr: aresta condicional SEM regra = "roda sempre" ──
# No engine, condicional sem expr é idêntica a sequential (sempre passa) — a
# causa do fan-out indevido. get_topology expõe os ids p/ a UI avisar.

def _cond_edge(eid, src, tgt, expr=None, type="conditional", config=None):
    e = {"id": eid, "source": src, "target": tgt, "type": type}
    if config is not None:
        e["config"] = config           # str JSON (como vem do DB) OU dict
    elif expr is not None:
        e["config"] = {"expr": expr}
    return e


def test_empty_expr_flagged():
    assert _conditional_without_expr([_cond_edge("e1", "R", "A", expr="")]) == ["e1"]


def test_whitespace_expr_flagged():
    assert _conditional_without_expr([_cond_edge("e1", "R", "A", expr="   ")]) == ["e1"]


def test_missing_config_flagged():
    edges = [{"id": "e1", "source": "R", "target": "A", "type": "conditional"}]
    assert _conditional_without_expr(edges) == ["e1"]


def test_present_expr_not_flagged():
    edges = [_cond_edge("e1", "R", "A", expr="inputs.tipo == 'imagem'")]
    assert _conditional_without_expr(edges) == []


def test_json_string_config_parsed():
    # config vem do DB como string JSON — precisa ser parseada
    edges = [
        _cond_edge("e1", "R", "A", config='{"expr": ""}'),
        _cond_edge("e2", "R", "B", config='{"expr": "x > 1"}'),
    ]
    assert _conditional_without_expr(edges) == ["e1"]


def test_malformed_json_config_treated_as_empty():
    edges = [_cond_edge("e1", "R", "A", config="not json")]
    assert _conditional_without_expr(edges) == ["e1"]


def test_non_conditional_never_flagged():
    # sequential/default sem regra NÃO são sinalizados — só conditional importa
    edges = [
        _cond_edge("e1", "R", "A", type="sequential"),
        _cond_edge("e2", "R", "B", type="default"),
    ]
    assert _conditional_without_expr(edges) == []
