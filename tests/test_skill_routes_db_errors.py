"""Regressão pro bug 'POST /api/v1/skills → 500 silencioso quando URN duplicado'.

Cenário real: Wizard IA gera URNs genéricos (`knowledge-base-query`,
`data-analysis`, etc.) e o user clica em "Criar Skill" duas vezes — a segunda
tentativa estourava `UniqueViolationError` no asyncpg, FastAPI convertia em
500 com `detail` vazio, e o user via apenas "req_<id>" no canto sem saber
o que aconteceu.

Esses testes mockam `skills_repo.create` lançando exceções típicas do
Postgres e verificam que o handler converte em:
- duplicate key (urn UNIQUE)        → 409 com mensagem que cita o URN
- check constraint (kind/stability) → 422 com mensagem indicando os enums
- erro genérico                     → 500 (preserva comportamento antigo)
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import skills_repo
from app.routes.skills import router as skills_router


def _make_client(*, raise_unhandled: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(skills_router)
    # raise_server_exceptions=False replica o que FastAPI faz em produção:
    # exception não tratada vira HTTP 500. Default do TestClient é propagar,
    # o que serve pra debug mas não pra testar o que o user vê.
    return TestClient(app, raise_server_exceptions=raise_unhandled)


_VALID_SKILL_MD = """---
id: urn:skill:geral:subagent:knowledge-base-query
version: 0.1.0
kind: subagent
owner: equipe-ia
stability: alpha
---

# Knowledge Base Query

## Purpose
Consulta KB.

## Activation Criteria
Sempre.

## Inputs
{}

## Workflow
1. busca

## Tool Bindings
- search_kb

## Output Contract
{}

## Failure Modes
- timeout
"""


class TestCreateSkillDbErrors:
    def test_duplicate_urn_returns_409_with_actionable_message(self, monkeypatch):
        """Postgres unique violation em `urn` → 409, não 500.

        Mensagem cita o URN e sugere a ação (mudar slug ou subir version)
        — sem isso o user só vê "req_<id>" e fica perdido.
        """
        async def boom(_data):
            raise Exception(
                'duplicate key value violates unique constraint "skills_urn_key"'
            )
        monkeypatch.setattr(skills_repo, "create", boom)

        r = _make_client().post(
            "/api/v1/skills",
            json={"raw_content": _VALID_SKILL_MD, "tags": "[]"},
        )
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "urn:skill:geral:subagent:knowledge-base-query" in detail
        assert "slug" in detail.lower() or "version" in detail.lower()

    def test_check_violation_returns_422(self, monkeypatch):
        """CHECK constraint (kind/stability fora do enum) → 422, não 500.

        Acontece se o parser deixar passar algum valor inválido — defesa em
        profundidade.
        """
        async def boom(_data):
            raise Exception(
                'new row for relation "skills" violates check constraint "skills_kind_check"'
            )
        monkeypatch.setattr(skills_repo, "create", boom)

        r = _make_client().post(
            "/api/v1/skills",
            json={"raw_content": _VALID_SKILL_MD, "tags": "[]"},
        )
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert "kind" in detail.lower() or "stability" in detail.lower()

    def test_undefined_column_returns_503_with_column_name(self, monkeypatch):
        """Schema drift (coluna faltante) → 503 com mensagem indicando a coluna
        e o SQL pra corrigir. Bug histórico Onda Tabular: deploy de schema novo
        sem rodar migration estourava 500 silencioso.
        """
        async def boom(_data):
            raise Exception(
                'column "data_tables" of relation "skills" does not exist'
            )
        monkeypatch.setattr(skills_repo, "create", boom)

        r = _make_client().post(
            "/api/v1/skills",
            json={"raw_content": _VALID_SKILL_MD, "tags": "[]"},
        )
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        # Cita a coluna exata e o comando pra resolver
        assert "data_tables" in detail
        assert "ALTER TABLE" in detail

    def test_unknown_db_error_still_500(self, monkeypatch):
        """Erros não classificados continuam 500 — não silenciamos coisa nova."""
        async def boom(_data):
            raise Exception("connection refused")
        monkeypatch.setattr(skills_repo, "create", boom)

        r = _make_client().post(
            "/api/v1/skills",
            json={"raw_content": _VALID_SKILL_MD, "tags": "[]"},
        )
        assert r.status_code == 500


class TestUpdateSkillDbErrors:
    def test_update_unique_violation_returns_409(self, monkeypatch):
        existing = {"id": "skill-1", "version": "0.1.0", "tags": "[]"}

        async def find_by_id(_id):
            return existing

        async def boom_update(_id, _data):
            raise Exception("duplicate key value violates unique constraint")

        monkeypatch.setattr(skills_repo, "find_by_id", find_by_id)
        monkeypatch.setattr(skills_repo, "update", boom_update)

        r = _make_client().put(
            "/api/v1/skills/skill-1",
            json={"raw_content": _VALID_SKILL_MD, "tags": "[]"},
        )
        assert r.status_code == 409, r.text
