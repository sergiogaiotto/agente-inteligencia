"""Invariante de desempenho: o invoke NUNCA espera por escrita de analytics.

Auditoria + atribuição por-key + débito de custo saem do CAMINHO DE RESPOSTA e
rodam DETACHED (fire-and-forget), drenados no shutdown. O gate de orçamento segue
síncrono e ANTES da execução. Pool asyncpg endurecido (max + command_timeout) p/
carga concorrente de API externa.
"""
import json
from pathlib import Path

import pytest

from app.routes import pipelines as pl


@pytest.mark.asyncio
async def test_schedule_nao_roda_no_caminho__so_no_drain():
    ran = {"n": 0}

    async def _work():
        ran["n"] += 1

    pl._schedule_analytics(_work())
    assert ran["n"] == 0                       # fire-and-forget: NÃO rodou síncrono
    await pl.drain_invoke_analytics(timeout=2.0)
    assert ran["n"] == 1                        # roda fora do caminho de resposta


@pytest.mark.asyncio
async def test_falha_de_analytics_e_engolida():
    async def _boom():
        raise RuntimeError("db down")

    pl._schedule_analytics(_boom())
    await pl.drain_invoke_analytics(timeout=2.0)  # _safe_analytics engole — não levanta


@pytest.mark.asyncio
async def test_record_consolida_auditoria_atribuicao_debito(monkeypatch):
    seen = {}

    async def _audit(row):
        seen["audit"] = row

    async def _attr(kid, kname, iid):
        seen["attr"] = (kid, kname, iid)

    async def _debit(kid, pid, result):
        seen["debit"] = (kid, pid, (result or {}).get("interaction_id"))

    monkeypatch.setattr(pl.audit_repo, "create", _audit)
    monkeypatch.setattr(pl, "_attribute_interaction_to_key", _attr)
    monkeypatch.setattr(pl, "_debit_api_key_cost", _debit)

    await pl._record_invoke_analytics(
        pid="p1", root="r", member_count=2,
        result={"interaction_id": "int1", "completed_agents": 2},
        api_key_id="k9", api_key_name="fe", actor_user_id="u1",
        arg_keys=["cd_cliente"], stream=True,
    )
    assert seen["attr"] == ("k9", "fe", "int1")
    assert seen["debit"] == ("k9", "p1", "int1")
    d = json.loads(seen["audit"]["details"])
    assert d["stream"] is True and d["api_key_id"] == "k9"
    assert d["arg_keys"] == ["cd_cliente"] and d["interaction_id"] == "int1"


def test_invoke_offloads_analytics_nos_dois_caminhos():
    src = Path("app/routes/pipelines.py").read_text(encoding="utf-8")
    # sync + stream agendam analytics em vez de await direto (2 call sites)
    assert src.count("_schedule_analytics(_record_invoke_analytics(") >= 2
    # o GATE de orçamento continua SÍNCRONO e antes da execução (enforcement duro)
    assert "await _guard_api_key_cost_budget(request)" in src
    # os helpers recebem valores extraídos (não o request) → chamáveis detached
    assert "async def _attribute_interaction_to_key(api_key_id, api_key_name, interaction_id" in src
    assert "async def _debit_api_key_cost(api_key_id, pid: str, result: dict)" in src


def test_drain_no_shutdown():
    main = Path("app/main.py").read_text(encoding="utf-8")
    assert "drain_invoke_analytics" in main


def test_pool_endurecido_e_tunavel():
    from app.core.config import Settings
    # defaults endurecidos (lidos do campo, imunes ao .env do dev)
    assert Settings.model_fields["database_pool_max"].default == 20
    assert Settings.model_fields["database_pool_min"].default == 5
    assert Settings.model_fields["database_command_timeout"].default == 60
    db = Path("app/core/database.py").read_text(encoding="utf-8")
    assert "command_timeout=settings.database_command_timeout" in db
    # o template de deploy também vem endurecido (senão o env pinaria o valor antigo)
    envex = Path(".env.example").read_text(encoding="utf-8")
    assert "DATABASE_POOL_MAX=20" in envex and "DATABASE_COMMAND_TIMEOUT=60" in envex
