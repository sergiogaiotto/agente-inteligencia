"""Guard de regressão de latência/amplificação do invoke de pipeline.

Complementa o harness scripts/latency_bench.py com asserts ESTÁVEIS (não
dependem da latência do LLM, que varia): número de round-trips ao Postgres
por invoke e presença do contrato de resposta. É o instrumento de
não-regressão do plano de tuning (Onda 2.3 aperta o teto).

Requer app docker de pé + Postgres acessível via `docker exec agente_postgres`.
"""
from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = pytest.mark.e2e

# Pipeline Aurora seedado (mesmo do harness). O invoke 'limite' é declarativo.
_PIPELINE = "8df2d21e-8417-4ac1-9610-bbadaf7f005d"
# Teto de round-trips por invoke LEVE.
# - Baseline (v25.1.x) ≈ 246-296 (amplificação: engine re-consultava
#   topologia/agents e o _order_col introspecionava information_schema).
# - PR-1 (v25.2.0, cache de topologia/schema) derrubou p/ ~155-195.
# Teto abaixo do baseline (prova o ganho travado) com margem p/ ruído do
# xact_commit sob tráfego concorrente. Follow-up (reuso de conexão por
# request) aperta mais.
_ROUNDTRIP_CEILING = 230


def _xact_commit() -> int:
    out = subprocess.run(
        ["docker", "exec", "agente_postgres", "psql", "-U", "agente",
         "-d", "agente_inteligencia", "-t", "-c",
         "SELECT xact_commit FROM pg_stat_database WHERE datname='agente_inteligencia';"],
        capture_output=True, text=True, timeout=20,
    ).stdout.strip()
    return int(out) if out.lstrip("-").isdigit() else -1


def test_invoke_leve_nao_amplifica_queries_alem_do_teto(api):
    """Um invoke declarativo (path 'limite') não deve estourar o teto de
    round-trips ao Postgres — pega regressão de amplificação de queries."""
    body = {"args": {"tipo": "limite", "mensagem": "Qual o limite do cliente 1001?",
                     "cd_cliente": 1001}}
    # warm (aquece caches de rota/provider/skill)
    api.post(f"/api/v1/pipelines/{_PIPELINE}/invoke", json=body)

    xc0 = _xact_commit()
    r = api.post(f"/api/v1/pipelines/{_PIPELINE}/invoke", json=body)
    xc1 = _xact_commit()
    assert r.status_code == 200, r.text

    if xc0 < 0 or xc1 < 0:
        pytest.skip("pg_stat_database indisponível — guard de round-trips pulado.")
    roundtrips = xc1 - xc0
    assert roundtrips <= _ROUNDTRIP_CEILING, (
        f"invoke leve fez {roundtrips} round-trips ao Postgres "
        f"(teto {_ROUNDTRIP_CEILING}) — regressão de amplificação de queries. "
        f"Se a Onda 2.3 foi implementada, o teto deveria ter CAÍDO, não subido."
    )


def test_invoke_contrato_de_resposta(api):
    """O invoke devolve o contrato completo (não regride sob tuning)."""
    body = {"args": {"tipo": "limite", "mensagem": "limite do cliente 1001?",
                     "cd_cliente": 1001}}
    r = api.post(f"/api/v1/pipelines/{_PIPELINE}/invoke", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("pipeline_id", "output", "final_state", "completed_agents",
              "pipeline_steps", "interaction_id"):
        assert k in d, f"contrato do invoke sem '{k}': {list(d)}"
    assert d["pipeline_id"] == _PIPELINE
    assert isinstance(d["pipeline_steps"], list)
