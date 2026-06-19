"""Mesh — regras condicionais: ciclo determinístico pelos endpoints reais.

NOTA DE ESCOPO: o CANVAS do Fluxograma (arrastar o port de um agente até outro
para conectar) é uma interação SVG/auto-layout intrinsecamente FRÁGIL para E2E de
browser — sob "fit-to-view" os nós ficam próximos e o alvo do arraste fica
ambíguo (verificado empiricamente: o arraste abria o editor no nó errado de forma
intermitente). Um teste flaky contraria o objetivo de suíte estável, então NÃO
dirigimos o arraste aqui.

Em vez disso cobrimos o MOTOR de "regras condicionais" ponta-a-ponta pelos mesmos
endpoints que a UI dirige — determinístico e estável:
  • o SIMULADOR de regra (Jinja) avalia corretamente contra dados de exemplo;
  • criar uma conexão CONDICIONAL persiste e aparece na topologia.

O render da página /mesh/flow continua coberto pelo smoke (carrega sem erro de
JS). Os data-testid do editor (node-port, conn-type-*, intent-keyword, kw-input,
add-clause, conn-save) ficam no template para um futuro teste de canvas com um
gancho de testabilidade estável.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e


def test_simulador_de_regra_condicional_avalia_jinja(api):
    """A regra Jinja 'roda só quando a resposta menciona pix' avalia certo."""
    expr = "'pix' in output_lower"

    r_match = api.post("/api/v1/mesh/connections/test-conditional", json={
        "expr": expr, "output": "Vou pagar por pix", "input": "",
        "final_state": "Recommend", "attachments": [],
    })
    assert r_match.status_code == 200, r_match.text
    assert r_match.json().get("result") is True

    r_no = api.post("/api/v1/mesh/connections/test-conditional", json={
        "expr": expr, "output": "olá, tudo bem", "input": "",
        "final_state": "Recommend", "attachments": [],
    })
    assert r_no.status_code == 200, r_no.text
    assert r_no.json().get("result") is False


def test_criar_conexao_condicional_aparece_na_topologia(api):
    ids = []
    for _ in range(2):
        r = api.post("/api/v1/agents", json={
            "name": f"E2E Cond {uuid.uuid4().hex[:6]}", "kind": "subagent",
            "task_type": "instruct",
            "system_prompt": "Agente descartável p/ teste E2E de regra condicional.",
        })
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["id"])
    src, tgt = ids
    conn_id = None
    try:
        r = api.post("/api/v1/mesh/connections", json={
            "source_agent_id": src, "target_agent_id": tgt,
            "connection_type": "conditional",
            "config": json.dumps({"expr": "'pix' in output_lower"}),
        })
        assert r.status_code in (200, 201), r.text
        conn_id = r.json().get("id")

        topo = api.get("/api/v1/mesh/topology").json()
        edge = next(
            (e for e in topo.get("edges", [])
             if e.get("source") == src and e.get("target") == tgt), None
        )
        assert edge is not None, "conexão condicional não apareceu na topologia"
        assert (edge.get("type") or "").lower() == "conditional"
    finally:
        if conn_id:
            try:
                api.delete(f"/api/v1/mesh/connections/{conn_id}")
            except Exception:
                pass
        for a in ids:
            try:
                api.delete(f"/api/v1/agents/{a}")
            except Exception:
                pass
