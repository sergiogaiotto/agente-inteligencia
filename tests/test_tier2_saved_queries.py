"""Tier 2 — saved_queries: curadoria humana + endpoints (PR4).

Unit do writer (`apply_saved_query`) com FakeRepo + endpoints PUT/GET/DELETE
(monkeypatched, sem DB real) + smokes de schema/UI. O smoke JSONB+asyncpg com
Postgres real roda no container (não no host) — ver o commit/PR.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

import app.routes.data_tables as dtr


class FakeRepo:
    """saved_queries_repo em memória (espelha a interface do Repository genérico)."""

    def __init__(self):
        self.store: dict = {}

    async def find_by_id(self, id):
        r = self.store.get(id)
        return dict(r) if r else None

    async def create(self, data):
        self.store[data["id"]] = dict(data)
        return data

    async def update(self, id, data):
        self.store.setdefault(id, {"id": id}).update(data)
        return dict(self.store[id])

    async def find_all(self, limit=100, **filters):
        return [
            dict(r) for r in self.store.values()
            if all(r.get(k) == v for k, v in filters.items())
        ]

    async def delete(self, id):
        return self.store.pop(id, None) is not None


@pytest.fixture
def fake_repo(monkeypatch):
    repo = FakeRepo()
    monkeypatch.setattr("app.data_tables.saved_queries.saved_queries_repo", repo)
    return repo


def _table_row(table_id="t1"):
    return {
        "id": table_id,
        "name": "Clientes",
        "catalog": {
            "table": {"description": ""},
            "columns": [
                {"name": "id", "type": "BIGINT", "description": "", "pii_category": "none", "source": "human"},
                {"name": "uf", "type": "VARCHAR", "description": "", "pii_category": "none", "source": "human"},
                {"name": "cpf", "type": "VARCHAR", "description": "", "pii_category": "cpf", "source": "human"},
            ],
        },
    }


# ─── serialize ───────────────────────────────────────────────────


def test_serialize_decodes_jsonb_string():
    from app.data_tables.saved_queries import serialize_saved_query
    out = serialize_saved_query({"id": "1", "query_json": '{"select":["a"]}', "pii_columns_allowed": '["cpf"]'})
    assert out["query_json"] == {"select": ["a"]}
    assert out["pii_columns_allowed"] == ["cpf"]
    out2 = serialize_saved_query({"id": "2", "query_json": {"select": []}, "pii_columns_allowed": None})
    assert out2["query_json"] == {"select": []}
    assert out2["pii_columns_allowed"] == []


# ─── apply_saved_query (writer único) ────────────────────────────


@pytest.mark.asyncio
async def test_apply_saved_query_approves_validates_and_redacts(fake_repo):
    from app.data_tables.saved_queries import apply_saved_query
    compiled = {"select": ["id", "uf", "cpf"], "filters": [{"col": "cpf", "op": "=", "value": "x"}], "order_by": [], "limit": 50}
    out = await apply_saved_query(
        _table_row(), "Clientes RS", "liste clientes do cpf 111.222.333-44", compiled, [], {"id": "u1"}
    )
    assert out["status"] == "approved" and out["source"] == "human"
    # cpf (PII) fora do select E do filtro (deny)
    assert out["query_json"]["select"] == ["id", "uf"]
    assert out["query_json"]["filters"] == []
    assert any("cpf" in b.lower() for b in out["blocked"])
    # pergunta redatada — CPF cru não persiste
    assert "111.222.333-44" not in out["question_nl"]
    assert "[CPF]" in out["question_nl"]
    # persistido com json.dumps (string no repo → asyncpg-safe)
    stored = fake_repo.store[out["id"]]
    assert isinstance(stored["query_json"], str)
    assert isinstance(stored["pii_columns_allowed"], str)
    assert stored["curated_by"] == "u1"


@pytest.mark.asyncio
async def test_apply_saved_query_empty_after_validation_400(fake_repo):
    from app.data_tables.saved_queries import apply_saved_query
    from app.evidence.tabular import TabularError
    compiled = {"select": ["cpf"], "filters": [{"col": "cpf", "op": "=", "value": "x"}], "limit": 10}
    with pytest.raises(TabularError) as ei:
        await apply_saved_query(_table_row(), "x", "q", compiled, [], {"id": "u"})
    assert ei.value.status_code == 400
    assert not fake_repo.store  # nada persistido


@pytest.mark.asyncio
async def test_apply_saved_query_approved_pii_eq_allowed(fake_repo):
    from app.data_tables.saved_queries import apply_saved_query
    compiled = {"select": ["id"], "filters": [{"col": "cpf", "op": "=", "value": "123"}], "limit": 10}
    out = await apply_saved_query(_table_row(), "x", "q", compiled, ["cpf"], {"id": "u"})
    assert out["query_json"]["filters"] == [{"col": "cpf", "op": "=", "value": "123"}]
    assert out["pii_columns_allowed"] == ["cpf"]


@pytest.mark.asyncio
async def test_apply_saved_query_update_existing(fake_repo):
    from app.data_tables.saved_queries import apply_saved_query
    first = await apply_saved_query(_table_row(), "v1", "q", {"select": ["id"], "limit": 10}, [], {"id": "u"})
    sid = first["id"]
    second = await apply_saved_query(
        _table_row(), "v2", "q2", {"select": ["uf"], "limit": 5}, [], {"id": "u"}, saved_query_id=sid
    )
    assert second["id"] == sid and second["name"] == "v2"
    assert second["query_json"]["select"] == ["uf"]
    assert len(fake_repo.store) == 1  # atualizou, não criou novo


@pytest.mark.asyncio
async def test_list_saved_queries_decodes(fake_repo):
    from app.data_tables.saved_queries import apply_saved_query, list_saved_queries
    await apply_saved_query(_table_row("t1"), "a", "q", {"select": ["id"]}, [], {"id": "u"})
    await apply_saved_query(_table_row("t1"), "b", "q", {"select": ["uf"]}, [], {"id": "u"})
    await apply_saved_query(_table_row("t2"), "c", "q", {"select": ["id"]}, [], {"id": "u"})
    rows = await list_saved_queries("t1")
    assert len(rows) == 2
    assert all(isinstance(r["query_json"], dict) for r in rows)


# ─── Endpoints PUT/GET/DELETE (writer real + FakeRepo) ───────────


def _patch(monkeypatch, *, flag=True, row=None, can_see=True):
    monkeypatch.setattr(dtr, "text_to_sql_enabled", lambda: flag)

    async def find(tid):
        return row if row is not None else _table_row(tid)

    monkeypatch.setattr(dtr, "find_by_id_with_ks", find)
    monkeypatch.setattr(dtr, "can_user_see", lambda u, r: can_see)

    async def audit(*a, **k):
        return None

    monkeypatch.setattr(dtr, "_audit", audit)


@pytest.mark.asyncio
async def test_put_endpoint_creates_and_validates(monkeypatch, fake_repo):
    _patch(monkeypatch)
    body = dtr.SavedQueryPutRequest(name="x", question="q", compiled={"select": ["id", "cpf"], "limit": 10})
    out = await dtr.put_saved_query_endpoint("t1", body, {"id": "u"})
    assert out["ok"] is True
    assert out["saved_query"]["query_json"]["select"] == ["id"]  # cpf descartado


@pytest.mark.asyncio
async def test_endpoints_flag_off_404(monkeypatch, fake_repo):
    _patch(monkeypatch, flag=False)
    with pytest.raises(HTTPException) as ei:
        await dtr.list_saved_queries_endpoint("t1", {"id": "u"})
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_endpoints_forbidden_403(monkeypatch, fake_repo):
    _patch(monkeypatch, can_see=False)
    with pytest.raises(HTTPException) as ei:
        await dtr.list_saved_queries_endpoint("t1", {"id": "u"})
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_list_and_delete_endpoint_roundtrip(monkeypatch, fake_repo):
    _patch(monkeypatch)
    body = dtr.SavedQueryPutRequest(name="x", question="q", compiled={"select": ["id"], "limit": 10})
    saved = (await dtr.put_saved_query_endpoint("t1", body, {"id": "u"}))["saved_query"]
    listed = await dtr.list_saved_queries_endpoint("t1", {"id": "u"})
    assert len(listed["saved_queries"]) == 1
    deleted = await dtr.delete_saved_query_endpoint("t1", saved["id"], {"id": "u"})
    assert deleted["deleted"] is True
    assert (await dtr.list_saved_queries_endpoint("t1", {"id": "u"}))["saved_queries"] == []


@pytest.mark.asyncio
async def test_delete_endpoint_wrong_table_404(monkeypatch, fake_repo):
    _patch(monkeypatch)
    body = dtr.SavedQueryPutRequest(name="x", question="q", compiled={"select": ["id"]})
    saved = (await dtr.put_saved_query_endpoint("t1", body, {"id": "u"}))["saved_query"]
    with pytest.raises(HTTPException) as ei:
        await dtr.delete_saved_query_endpoint("t2", saved["id"], {"id": "u"})
    assert ei.value.status_code == 404


# ─── Smokes de schema + UI (puros) ───────────────────────────────


def test_schema_has_saved_queries_and_log_columns():
    from app.core import database as db
    assert "CREATE TABLE IF NOT EXISTS saved_queries" in db.SCHEMA
    migs = " ".join(db._IDEMPOTENT_MIGRATIONS)
    assert "data_table_query_logs ADD COLUMN IF NOT EXISTS nl_question" in migs
    assert "nl_generated_struct" in migs
    assert "masked_columns" in migs
    assert db.saved_queries_repo.table == "saved_queries"


def test_main_exposes_text_to_sql_jinja_global():
    from app.main import app
    g = app.state.templates.env.globals
    assert "text_to_sql_enabled" in g and callable(g["text_to_sql_enabled"])


def test_evidence_template_has_ask_tab():
    from pathlib import Path
    content = Path("app/templates/pages/evidence.html").read_text(encoding="utf-8")
    assert "text_to_sql_enabled()" in content   # aba gated por Jinja
    assert "compileAsk()" in content
    assert "saveAsk()" in content
    assert "/saved-queries" in content
    assert "Perguntar" in content


def test_help_content_label_fixed():
    from pathlib import Path
    content = Path("app/static/js/help-content.js").read_text(encoding="utf-8")
    assert "SQL parametrizado via DuckDB" in content
    assert "text-to-SQL via DuckDB" not in content
