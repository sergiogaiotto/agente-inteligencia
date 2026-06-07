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

from app.routes.mesh import _fanout_roots


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
