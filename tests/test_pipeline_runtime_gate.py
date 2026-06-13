"""Testes do gate de runtime por status de pipeline (PR2).

Decisão travada (2026-06-12): SÓ 'aposentado' bloqueia, e SÓ na ENTRADA.
'rascunho'/'publicado'/sem-pipeline NÃO afetam o runtime. A cadeia downstream
não é gateada (só o entry agent é checado).

Estratégia: isolar o gate sem rodar LLM/DB. Monkeypatcham-se os singletons
compartilhados (agents_repo/pipeline_membership/pipelines_repo/audit_repo) e a
resolução de cadeia `_resolve_ordered_chain_with_parents` é trocada por uma
SENTINELA que levanta RuntimeError("REACHED_CHAIN"). Assim:
  - aposentado  → execute_pipeline levanta ValueError("...aposentado...") ANTES
    de chegar na sentinela (gate bloqueou);
  - demais      → a sentinela é alcançada (gate deixou passar).
"""
import asyncio

import pytest

import app.core.database as db
import app.agents.engine as engine
from app.agents.engine import execute_pipeline


def _async(value=None, *, raises=None):
    async def _fn(*a, **k):
        if raises is not None:
            raise raises
        return value
    return _fn


def _setup(monkeypatch, *, pipeline_id, pipeline_row):
    """Prepara o ambiente mínimo: entry agent válido, membership e pipeline row
    configuráveis, audit no-op, e a resolução de cadeia como sentinela."""
    monkeypatch.setattr(
        db.agents_repo, "find_by_id",
        _async({"id": "entry", "name": "Entry", "status": "active"}),
    )
    monkeypatch.setattr(db.pipeline_membership, "pipeline_of", _async(pipeline_id))
    monkeypatch.setattr(db.pipelines_repo, "find_by_id", _async(pipeline_row))
    monkeypatch.setattr(db.audit_repo, "create", _async({}))
    # Sentinela: se o gate deixar passar, isto é alcançado e levanta REACHED_CHAIN.
    monkeypatch.setattr(
        engine, "_resolve_ordered_chain_with_parents",
        _async(raises=RuntimeError("REACHED_CHAIN")),
    )


def test_aposentado_blocks_at_entry(monkeypatch):
    _setup(monkeypatch, pipeline_id="p1", pipeline_row={"id": "p1", "name": "P Aposentado", "status": "aposentado"})
    with pytest.raises(ValueError, match="aposentado"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_aposentado_block_names_the_pipeline(monkeypatch):
    _setup(monkeypatch, pipeline_id="p1", pipeline_row={"id": "p1", "name": "Folha de Pagamento", "status": "aposentado"})
    with pytest.raises(ValueError, match="Folha de Pagamento"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_publicado_passes_gate(monkeypatch):
    # Gate deixa passar → alcança a sentinela (não é o ValueError de aposentado).
    _setup(monkeypatch, pipeline_id="p1", pipeline_row={"id": "p1", "name": "P", "status": "publicado"})
    with pytest.raises(RuntimeError, match="REACHED_CHAIN"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_rascunho_passes_gate(monkeypatch):
    # Regressão-zero p/ grupos migrados (viraram 'rascunho' no PR1): rascunho roda.
    _setup(monkeypatch, pipeline_id="p1", pipeline_row={"id": "p1", "name": "P", "status": "rascunho"})
    with pytest.raises(RuntimeError, match="REACHED_CHAIN"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_no_pipeline_passes_gate(monkeypatch):
    # Entry sem pipeline (caso majoritário do mesh hoje) → roda normal.
    _setup(monkeypatch, pipeline_id=None, pipeline_row=None)
    with pytest.raises(RuntimeError, match="REACHED_CHAIN"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_orphan_membership_passes_gate(monkeypatch):
    # Membership aponta p/ pipeline inexistente (find_by_id=None) → defensivo: roda.
    _setup(monkeypatch, pipeline_id="ghost", pipeline_row=None)
    with pytest.raises(RuntimeError, match="REACHED_CHAIN"):
        asyncio.run(execute_pipeline("entry", "oi"))


def test_lookup_failure_is_fail_open(monkeypatch):
    # FAIL-OPEN: se a resolução do pipeline levantar (ex.: pool indisponível em
    # testes que não mockam o DB), o gate NÃO bloqueia — alcança a sentinela.
    _setup(monkeypatch, pipeline_id="p1", pipeline_row={"id": "p1", "name": "P", "status": "publicado"})
    monkeypatch.setattr(
        db.pipeline_membership, "pipeline_of",
        _async(raises=RuntimeError("Pool PostgreSQL não inicializado")),
    )
    with pytest.raises(RuntimeError, match="REACHED_CHAIN"):
        asyncio.run(execute_pipeline("entry", "oi"))
