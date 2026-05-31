"""PR #229 — proteção contra duplo cifragem de auth_token de MCP tools.

# Bug original

Operador reportou: "as chaves dos MCPs não estão sendo mantidas e parecem
estar trocando entre tools cadastrados". Investigação revelou bug crítico:

1. `GET /api/v1/tools/{id}` retornava `auth_token` com ciphertext (`fernet:gAAA…`).
2. UI populava `<input x-model="form.auth_token">` com esse ciphertext.
3. Operador editava outro campo (descrição, custo, etc.) e clicava Atualizar.
4. UI enviava o payload INCLUINDO o ciphertext em `auth_token`.
5. Backend chamava `write_secret(auth_token)` que delegava para `encrypt()`.
6. `encrypt()` **não detectava** valor já cifrado e cifrava de novo →
   token virava `fernet:fernet:gAAAA<duplo>` no banco.
7. Próxima chamada MCP: `read_secret` decifrava uma vez → obtinha
   `fernet:gAAAA<orig>` (não plaintext!) → `Authorization: Bearer fernet:…` → 401.

A cada save, mais uma camada de cifragem. Token efetivamente perdido.

# Fixes (este arquivo cobre)

1. `encrypt()` é IDEMPOTENTE — valor com prefixo `fernet:` passa direto.
   Defesa em profundidade no lower layer.
2. `GET /tools` e `GET /tools/{id}` mascaram `auth_token` e secrets do
   `auth_config` (client_secret/client_key/ca_cert) — substituem por ""
   e adicionam flags `has_auth_token` / `has_auth_config_secrets`.
3. `PUT /tools/{id}` preserva auth_token existente se cliente mandou vazio.
4. UI nunca recebe ciphertext, então nunca tem como reenviá-lo por engano.

# Estratégia dos testes

- Unitários para `encrypt()` puro (caso 1).
- TestClient para GET/PUT (casos 2 e 3) com mock de `tools_repo`.
- Mock _get_fernet via SECRET_KEY para tornar testes determinísticos.
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Garante uma master key estável para os testes ANTES de importar secrets
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-long-x")

from app.core import secrets as secrets_mod  # noqa: E402


# ─── 1. encrypt() é idempotente ──────────────────────────────


class TestEncryptIdempotent:
    def test_encrypt_plaintext_produces_ciphertext_with_prefix(self):
        out = secrets_mod.encrypt("my-real-token")
        assert out.startswith("fernet:")
        # Confirma que decifra de volta para o plaintext original
        assert secrets_mod.decrypt(out) == "my-real-token"

    def test_encrypt_is_idempotent_on_already_ciphered(self):
        """O cerne do bug: encrypt(encrypt(x)) deve == encrypt(x)."""
        once = secrets_mod.encrypt("abc-123")
        twice = secrets_mod.encrypt(once)
        # Sem o fix, twice teria duplo cifragem (fernet:fernet:gAAA...).
        assert once == twice, (
            "encrypt() não detectou ciphertext de entrada — vai gerar "
            "duplo cifragem e quebrar tokens de MCPs em produção."
        )
        # Decifra ainda volta para o plaintext original (não para o ciphertext interno)
        assert secrets_mod.decrypt(twice) == "abc-123"

    def test_write_secret_is_idempotent(self):
        """write_secret usa encrypt — deve herdar a idempotência."""
        once = secrets_mod.write_secret("xyz")
        twice = secrets_mod.write_secret(once)
        assert once == twice

    def test_empty_input_stays_empty(self):
        assert secrets_mod.encrypt("") == ""
        assert secrets_mod.write_secret("") == ""

    def test_encrypt_preserves_plaintext_round_trip_across_many_writes(self):
        """Simula 5 saves consecutivos da UI re-enviando o mesmo valor:
        valor armazenado nunca degrada e sempre decifra para o original."""
        plain = "token-original-do-operador"
        stored = secrets_mod.write_secret(plain)
        for _ in range(5):
            stored = secrets_mod.write_secret(stored)  # UI re-envia o ciphertext
        assert secrets_mod.read_secret(stored) == plain


# ─── 2. GET de tools mascara secrets ─────────────────────────


def _make_app(tools_store: dict, monkeypatch):
    """FastAPI mínimo com tools endpoints + mocks."""
    from app.routes import dashboard

    async def fake_find_all(limit=50, **kw):
        return list(tools_store.values())[:limit]

    async def fake_count(**kw):
        return len(tools_store)

    async def fake_find_by_id(tid):
        return tools_store.get(tid)

    async def fake_update(tid, data):
        cur = dict(tools_store.get(tid, {}))
        cur.update(data)
        tools_store[tid] = cur
        return cur

    async def fake_audit(data):
        return data

    monkeypatch.setattr(dashboard.tools_repo, "find_all", fake_find_all)
    monkeypatch.setattr(dashboard.tools_repo, "count", fake_count)
    monkeypatch.setattr(dashboard.tools_repo, "find_by_id", fake_find_by_id)
    monkeypatch.setattr(dashboard.tools_repo, "update", fake_update)
    monkeypatch.setattr(dashboard.audit_repo, "create", fake_audit)

    app = FastAPI()
    app.include_router(dashboard.router)
    return app


class TestGetMasksSecrets:
    def test_list_tools_masks_auth_token(self, monkeypatch):
        store = {
            "t1": {
                "id": "t1", "name": "Tavily",
                "mcp_server": "https://mcp.tavily.com/mcp",
                "auth_requirements": "api_key",
                "auth_token": "fernet:gAAAAxxxxxxxxxxxxxxx",
                "auth_config": "{}",
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.get("/api/v1/tools")
        assert r.status_code == 200
        tools = r.json()["tools"]
        assert tools[0]["auth_token"] == ""
        assert tools[0]["has_auth_token"] is True

    def test_get_tool_returns_empty_when_no_auth_token(self, monkeypatch):
        store = {
            "t2": {
                "id": "t2", "name": "Aberto", "mcp_server": "x",
                "auth_requirements": "", "auth_token": "",
                "auth_config": "{}",
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.get("/api/v1/tools/t2")
        body = r.json()
        assert body["auth_token"] == ""
        assert body["has_auth_token"] is False

    def test_get_tool_masks_oauth_secrets_in_auth_config(self, monkeypatch):
        import json as _json
        store = {
            "t3": {
                "id": "t3", "name": "OAuth tool", "mcp_server": "x",
                "auth_requirements": "oauth2", "auth_token": "",
                "auth_config": _json.dumps({
                    "client_id": "ci-1234",
                    "client_secret": "sk-secret-xyz",
                    "token_url": "https://auth.example.com/token",
                    "scope": "read",
                }),
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        body = client.get("/api/v1/tools/t3").json()
        cfg = _json.loads(body["auth_config"])
        assert cfg["client_id"] == "ci-1234"   # NÃO secret
        assert cfg["token_url"] == "https://auth.example.com/token"
        assert cfg["client_secret"] == ""      # mascarado
        assert body["has_auth_config_secrets"] is True


# ─── 3. PUT preserva auth_token vazio ────────────────────────


class TestPutPreservesAuthToken:
    def test_put_with_empty_auth_token_preserves_existing(self, monkeypatch):
        """Cenário canônico do bug: GET retorna mascarado, UI manda
        auth_token="" no PUT, backend NÃO deve sobrescrever."""
        store = {
            "t1": {
                "id": "t1", "name": "Tavily", "mcp_server": "x",
                "auth_requirements": "api_key",
                "auth_token": "fernet:gAAAA<existing>",
                "auth_config": "{}",
                "sensitivity": "internal",
                "requires_trusted_context": 0,
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.put(
            "/api/v1/tools/t1",
            json={
                "name": "Tavily",
                "mcp_server": "x",
                "description": "Nova descrição",
                "operations": "[]",
                "cost_per_call": 0,
                "sensitivity": "internal",
                "requires_trusted_context": False,
                "auth_requirements": "api_key",
                "auth_token": "",  # ← vazio!
                "auth_config": "{}",
            },
        )
        assert r.status_code == 200, r.text
        # Token original preservado no store
        assert store["t1"]["auth_token"] == "fernet:gAAAA<existing>"

    def test_put_with_new_auth_token_substitutes(self, monkeypatch):
        store = {
            "t1": {
                "id": "t1", "name": "Tavily", "mcp_server": "x",
                "auth_requirements": "api_key",
                "auth_token": "fernet:gAAAA<existing>",
                "auth_config": "{}",
                "sensitivity": "internal",
                "requires_trusted_context": 0,
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.put(
            "/api/v1/tools/t1",
            json={
                "name": "Tavily", "mcp_server": "x",
                "auth_requirements": "api_key",
                "auth_token": "novo-token-plaintext",
                "auth_config": "{}",
                "sensitivity": "internal",
                "cost_per_call": 0,
                "requires_trusted_context": False,
                "operations": "[]",
                "description": "",
            },
        )
        assert r.status_code == 200
        # Novo token cifrado no store
        new = store["t1"]["auth_token"]
        assert new.startswith("fernet:")
        assert new != "fernet:gAAAA<existing>"
        # E é o plaintext correto após decifrar
        assert secrets_mod.read_secret(new) == "novo-token-plaintext"

    def test_put_omitting_auth_token_field_entirely_preserves(self, monkeypatch):
        """Frontend novo (PR #229) deleta a key auth_token do payload
        quando vazia em edit. Pydantic exclude_unset garante que não chega
        ao update."""
        store = {
            "t1": {
                "id": "t1", "name": "Tavily", "mcp_server": "x",
                "auth_requirements": "api_key",
                "auth_token": "fernet:gAAAA<existing>",
                "auth_config": "{}",
                "sensitivity": "internal",
                "requires_trusted_context": 0,
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.put(
            "/api/v1/tools/t1",
            json={
                "description": "Sem mexer em auth",
                # auth_token deliberadamente OMITIDO
            },
        )
        assert r.status_code == 200
        assert store["t1"]["auth_token"] == "fernet:gAAAA<existing>"

    def test_put_preserves_oauth_secret_when_ui_sends_empty(self, monkeypatch):
        import json as _json
        store = {
            "t3": {
                "id": "t3", "name": "OAuth", "mcp_server": "x",
                "auth_requirements": "oauth2", "auth_token": "",
                "auth_config": _json.dumps({
                    "client_id": "ci", "client_secret": "real-secret",
                    "token_url": "https://x/token", "scope": "",
                }),
                "sensitivity": "internal",
                "requires_trusted_context": 0,
            },
        }
        client = TestClient(_make_app(store, monkeypatch))
        r = client.put(
            "/api/v1/tools/t3",
            json={
                "name": "OAuth", "mcp_server": "x",
                "auth_requirements": "oauth2",
                "auth_token": "",
                "auth_config": _json.dumps({
                    "client_id": "ci-CHANGED",   # mudou só client_id
                    "client_secret": "",          # mascarado, UI manda vazio
                    "token_url": "https://x/token", "scope": "",
                }),
                "sensitivity": "internal",
                "cost_per_call": 0,
                "requires_trusted_context": False,
                "operations": "[]",
                "description": "",
            },
        )
        assert r.status_code == 200
        cfg_final = _json.loads(store["t3"]["auth_config"])
        assert cfg_final["client_id"] == "ci-CHANGED"
        # client_secret preservado, NÃO zerado
        assert cfg_final["client_secret"] == "real-secret"


# ─── 4. Round-trip: simula o cenário real do bug original ────


class TestRoundTripDoesNotCorruptToken:
    def test_5_consecutive_puts_with_empty_auth_token_preserves_original(
        self, monkeypatch,
    ):
        """Cenário real reportado por operador: editar a tool 5x em
        sequência sem mexer no token NÃO pode corromper o ciphertext."""
        original_plain = "real-tavily-token-abc"
        original_cipher = secrets_mod.write_secret(original_plain)

        store = {
            "t1": {
                "id": "t1", "name": "Tavily", "mcp_server": "x",
                "auth_requirements": "api_key",
                "auth_token": original_cipher,
                "auth_config": "{}",
                "sensitivity": "internal",
                "requires_trusted_context": 0,
            },
        }
        client = TestClient(_make_app(store, monkeypatch))

        for i in range(5):
            r = client.put(
                "/api/v1/tools/t1",
                json={
                    "name": "Tavily", "mcp_server": "x",
                    "auth_requirements": "api_key",
                    "auth_token": "",  # vazio sempre
                    "auth_config": "{}",
                    "sensitivity": "internal",
                    "cost_per_call": 0,
                    "requires_trusted_context": False,
                    "operations": "[]",
                    "description": f"edit #{i}",
                },
            )
            assert r.status_code == 200

        # Após 5 saves: o token armazenado decifra para o plaintext original
        final = store["t1"]["auth_token"]
        assert secrets_mod.read_secret(final) == original_plain, (
            "Token corrompido após 5 PUTs com auth_token vazio. "
            "Bug original: o backend re-cifrava o ciphertext que a UI "
            "reenviava por engano, gerando duplo/triplo/N cifragem."
        )
