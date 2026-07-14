"""Onda 6 — escopo por API-key (allowed_pipeline_ids + read_only, 33.17.0).

Uma key invocava QUALQUER pipeline. Cobre o gate (read_only, allowed_pipeline_ids,
cookie=sem-restrição), o parse, o schema/revisão Alembic e a fiação.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.apikey_scope import _parse_allowed, assert_api_key_can_invoke


def _req(scope):
    return SimpleNamespace(state=SimpleNamespace(api_key_scope=scope, api_key_id="k1"))


class TestParseAllowed:
    def test_json_string(self):
        assert _parse_allowed('["a","b"]') == ["a", "b"]

    def test_list(self):
        assert _parse_allowed(["a"]) == ["a"]

    def test_none_e_lixo(self):
        assert _parse_allowed(None) == []
        assert _parse_allowed("nao-json") == []


class TestGate:
    def test_cookie_sem_escopo_passa(self):
        assert_api_key_can_invoke(_req(None), pipeline_id="p1")  # não levanta

    def test_read_only_403(self):
        with pytest.raises(HTTPException) as ei:
            assert_api_key_can_invoke(_req({"read_only": True}), pipeline_id="p1")
        assert ei.value.status_code == 403

    def test_pipeline_fora_da_lista_403(self):
        with pytest.raises(HTTPException) as ei:
            assert_api_key_can_invoke(
                _req({"read_only": False, "allowed_pipeline_ids": '["p2"]'}), pipeline_id="p1")
        assert ei.value.status_code == 403

    def test_pipeline_na_lista_passa(self):
        assert_api_key_can_invoke(
            _req({"read_only": False, "allowed_pipeline_ids": '["p1","p2"]'}), pipeline_id="p1")

    def test_allowed_vazio_libera_todos(self):
        assert_api_key_can_invoke(
            _req({"read_only": False, "allowed_pipeline_ids": None}), pipeline_id="qualquer")

    def test_key_escopada_bloqueia_invoke_de_agente(self):
        # 35.2.0 (fast-follow): key com allowed_pipeline_ids NÃO invoca agente
        # avulso (pipeline_id=None) — senão invocar direto o especialista do
        # pipeline driblaria o escopo. Antes era permitido (gap do #585).
        with pytest.raises(HTTPException) as ei:
            assert_api_key_can_invoke(_req({"read_only": False, "allowed_pipeline_ids": '["p2"]'}))
        assert ei.value.status_code == 403
        # sem escopo (allowed vazio), agente avulso segue liberado
        assert_api_key_can_invoke(_req({"read_only": False, "allowed_pipeline_ids": None}))
        # e read_only bloqueia sempre
        with pytest.raises(HTTPException):
            assert_api_key_can_invoke(_req({"read_only": True}))


class TestSchema:
    def test_colunas_no_schema(self):
        from app.core.database import SCHEMA
        assert "allowed_pipeline_ids TEXT" in SCHEMA
        assert "read_only BOOLEAN DEFAULT FALSE" in SCHEMA


def _load_rev0004():
    p = Path("alembic/versions/0004_api_keys_scope.py")
    spec = importlib.util.spec_from_file_location("rev0004_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAlembic0004:
    def test_chain_0003_para_0004(self):
        mod = _load_rev0004()
        assert mod.revision == "0004_api_keys_scope"
        assert mod.down_revision == "0003_interactions_owner_user_id"

    def test_upgrade_downgrade(self, monkeypatch):
        import alembic.op
        calls: list[str] = []
        monkeypatch.setattr(alembic.op, "execute", lambda sql: calls.append(sql))
        mod = _load_rev0004()
        mod.upgrade()
        up = "\n".join(calls)
        assert "ADD COLUMN IF NOT EXISTS allowed_pipeline_ids" in up
        assert "ADD COLUMN IF NOT EXISTS read_only" in up
        calls.clear()
        mod.downgrade()
        down = "\n".join(calls)
        assert "DROP COLUMN IF EXISTS read_only" in down
        assert "DROP COLUMN IF EXISTS allowed_pipeline_ids" in down


class TestWiring:
    def test_verify_api_key_usa_select_star(self):
        src = Path("app/core/auth_apikey.py").read_text(encoding="utf-8")
        assert "SELECT * FROM api_keys WHERE key_hash" in src

    def test_require_user_carimba_scope(self):
        src = Path("app/core/auth.py").read_text(encoding="utf-8")
        assert "request.state.api_key_scope" in src
        assert '"read_only"' in src

    def test_invoke_pipeline_e_agente_aplicam(self):
        assert "assert_api_key_can_invoke(request, pipeline_id=pid)" in \
            Path("app/routes/pipelines.py").read_text(encoding="utf-8")
        assert "assert_api_key_can_invoke(request)" in \
            Path("app/routes/agents.py").read_text(encoding="utf-8")

    def test_create_e_patch_persistem_scope(self):
        src = Path("app/routes/api_keys.py").read_text(encoding="utf-8")
        assert "allowed_pipeline_ids" in src and "read_only" in src
        assert '@router.patch("/{key_id}/scope")' in src
