"""Regressão pro bug 'POST /tools/test envia auth_token cifrado como Bearer'.

Cenário real (2026-05-27): user editou MCP do Tavily, viu campo API Key
preenchido com 'fernet:gAAAAA...' (valor lido do banco, mostrado pra
confirmação visual + botão Ocultar). Clicou 'Conectar e Descobrir' → o
form enviou o mesmo valor cifrado de volta pro backend → _build_mcp_auth
montou `Authorization: Bearer fernet:gAAAAA...` → Tavily HTTP 401.

Fix: _build_mcp_auth chama read_secret() em auth_token. Idempotente:
texto plano passa direto, fernet decifra.

Espelha o que mcp/runtime.py já fazia em invocação real (linha 702).
"""
from __future__ import annotations

import pytest

from app.core.secrets import encrypt
from app.routes.dashboard import _build_mcp_auth


class TestBuildMcpAuthDecrypt:
    def test_plain_token_passes_through(self):
        """Token em texto plano (legacy/dev) continua funcionando."""
        auth = _build_mcp_auth(auth_type="api_key", auth_token="tvly-real-key-abc")
        assert auth["headers"]["Authorization"] == "Bearer tvly-real-key-abc"

    def test_encrypted_token_is_decrypted_before_header(self):
        """Token com prefixo fernet: precisa ser decifrado ANTES de virar Bearer.

        Este era o bug do Tavily 401: o `Authorization: Bearer fernet:gAAAAA...`
        ia pro servidor MCP que rejeitava.
        """
        plain = "tvly-actual-secret-value"
        encrypted = encrypt(plain)  # gera "fernet:gAAAAA..."
        assert encrypted.startswith("fernet:"), "Setup do teste — encrypt deve usar prefixo"

        auth = _build_mcp_auth(auth_type="api_key", auth_token=encrypted)
        header = auth["headers"].get("Authorization", "")

        assert header == f"Bearer {plain}", (
            f"Authorization deveria ter o plaintext '{plain}', "
            f"mas veio '{header}' — _build_mcp_auth não está decifrando"
        )
        # Sanity extra: prefixo fernet NÃO pode estar no header
        assert "fernet:" not in header

    def test_empty_token_does_not_add_authorization(self):
        """Sem token não adiciona header de auth — vai sem autenticação."""
        auth = _build_mcp_auth(auth_type="api_key", auth_token="")
        assert "Authorization" not in auth["headers"]

    def test_whitespace_only_token_treated_as_empty(self):
        auth = _build_mcp_auth(auth_type="api_key", auth_token="   ")
        assert "Authorization" not in auth["headers"]

    def test_fallback_path_also_decrypts(self):
        """Tipo desconhecido + token: fallback monta Bearer. Também tem que
        descriptografar (mesma lógica)."""
        plain = "fallback-token-xyz"
        encrypted = encrypt(plain)
        auth = _build_mcp_auth(auth_type="some-future-type", auth_token=encrypted)
        # Quando auth_type desconhecido mas tem token, fallback aplica
        assert auth["headers"].get("Authorization") == f"Bearer {plain}"

    def test_no_auth_type_returns_base_headers(self):
        """Sem auth_type, retorna headers base (Content-Type + Accept) sem
        Authorization. Token irrelevante nesse caso — mas descriptografar não
        pode estourar erro."""
        encrypted = encrypt("anything")
        auth = _build_mcp_auth(auth_type="", auth_token=encrypted)
        assert "Authorization" not in auth["headers"]
        # Base headers preservados
        assert "Content-Type" in auth["headers"]
        assert "Accept" in auth["headers"]
