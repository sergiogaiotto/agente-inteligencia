"""Testes de app.core.http_auth — auth headers + body preparation."""

from __future__ import annotations

import base64
import os

import pytest

from app.core.http_auth import (
    BODY_TYPES,
    build_auth_headers,
    prepare_request_body,
    redact_headers,
)


@pytest.fixture(autouse=True)
def setup_crypto(monkeypatch):
    """Garante chave de crypto disponível para decrypt nos testes."""
    monkeypatch.setenv("MAESTRO_SECRET_KEY", "test-key")
    from app.core import crypto
    crypto._get_fernet.cache_clear()
    yield


class TestAuthHeaders:
    def test_none_retorna_vazio(self):
        assert build_auth_headers({"auth_type": "none"}) == {}

    def test_api_key_default_header(self):
        out = build_auth_headers({"auth_type": "api_key", "api_key": "abc123"})
        assert out == {"X-API-Key": "abc123"}

    def test_api_key_header_customizado(self):
        out = build_auth_headers({
            "auth_type": "api_key", "api_key": "abc", "auth_header": "X-Token",
        })
        assert out == {"X-Token": "abc"}

    def test_bearer(self):
        out = build_auth_headers({"auth_type": "bearer", "api_key": "token123"})
        assert out == {"Authorization": "Bearer token123"}

    def test_basic_codifica_b64(self):
        out = build_auth_headers({"auth_type": "basic", "api_key": "user:pass"})
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert out == {"Authorization": f"Basic {expected}"}

    def test_basic_unicode_em_credenciais(self):
        # Usuário com acento — deve encodar UTF-8 antes do base64
        out = build_auth_headers({"auth_type": "basic", "api_key": "joão:senh@"})
        expected = base64.b64encode("joão:senh@".encode("utf-8")).decode("ascii")
        assert out["Authorization"] == f"Basic {expected}"

    def test_cookie(self):
        out = build_auth_headers({"auth_type": "cookie", "api_key": "session=abc"})
        assert out == {"Cookie": "session=abc"}

    def test_api_key_vazia_retorna_vazio(self):
        # Sem api_key não há como autenticar — não envia headers
        assert build_auth_headers({"auth_type": "bearer", "api_key": ""}) == {}

    def test_auth_type_desconhecido(self):
        # Tipo inválido → vazio + warning (não levanta exception)
        out = build_auth_headers({"auth_type": "magico", "api_key": "x"})
        assert out == {}

    def test_secret_cifrado_eh_decifrado(self):
        # API key armazenada cifrada (enc::xxx) é decifrada antes de usar
        from app.core.crypto import encrypt_secret
        cifrado = encrypt_secret("super-secret")
        out = build_auth_headers({"auth_type": "bearer", "api_key": cifrado})
        assert out == {"Authorization": "Bearer super-secret"}


class TestRedactHeaders:
    def test_mascara_authorization(self):
        out = redact_headers({"Authorization": "Bearer abc", "X-Custom": "ok"})
        assert out["Authorization"] == "***"
        assert out["X-Custom"] == "ok"

    def test_case_insensitive(self):
        out = redact_headers({"authorization": "x", "AUTHORIZATION": "y"})
        assert out["authorization"] == "***"
        assert out["AUTHORIZATION"] == "***"

    def test_mascara_cookie_e_x_api_key(self):
        out = redact_headers({"Cookie": "sess=x", "X-API-Key": "abc", "Content-Type": "json"})
        assert out["Cookie"] == "***"
        assert out["X-API-Key"] == "***"
        assert out["Content-Type"] == "json"

    def test_vazio_ok(self):
        assert redact_headers({}) == {}
        assert redact_headers(None) == {}


class TestPrepareRequestBody:
    def test_body_types_constantes(self):
        # As 5 estratégias suportadas
        assert set(BODY_TYPES) == {"json", "form_urlencoded", "multipart", "text", "xml"}

    def test_json_default(self):
        out = prepare_request_body("json", {"foo": 1})
        assert out["json"] == {"foo": 1}
        # Content-Type fica para httpx auto

    def test_form_urlencoded_dict(self):
        out = prepare_request_body("form_urlencoded", {"a": "b", "c": "d"})
        assert out["data"] == {"a": "b", "c": "d"}

    def test_form_urlencoded_string_pre_codificada(self):
        out = prepare_request_body("form_urlencoded", "a=1&b=2")
        assert out["content"] == "a=1&b=2"
        assert out["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    def test_text(self):
        out = prepare_request_body("text", "Hello, world!")
        assert out["content"] == "Hello, world!"
        assert out["headers"]["Content-Type"].startswith("text/plain")

    def test_xml(self):
        out = prepare_request_body("xml", "<x>1</x>")
        assert out["content"] == "<x>1</x>"
        assert out["headers"]["Content-Type"].startswith("application/xml")

    def test_multipart_com_fields_e_files(self):
        out = prepare_request_body("multipart", {
            "fields": {"a": "b"},
            "files": [{"name": "doc", "content": "hello", "filename": "doc.txt"}],
        })
        assert out["data"] == {"a": "b"}
        assert len(out["files"]) == 1
        name, (filename, content, ct) = out["files"][0]
        assert name == "doc"
        assert filename == "doc.txt"
        assert content == b"hello"  # str → bytes

    def test_multipart_content_bytes_direto(self):
        out = prepare_request_body("multipart", {
            "files": [{"name": "img", "content": b"\x89PNG", "content_type": "image/png"}],
        })
        _, (_, content, ct) = out["files"][0]
        assert content == b"\x89PNG"
        assert ct == "image/png"

    def test_body_vazio_nao_envia(self):
        # None, '', {}, [] são tratados como "sem body"
        for v in (None, "", {}, []):
            out = prepare_request_body("json", v)
            assert "json" not in out and "data" not in out and "content" not in out

    def test_body_type_desconhecido_cai_em_json(self):
        out = prepare_request_body("yaml", {"x": 1})
        assert out["json"] == {"x": 1}

    def test_body_type_case_insensitive(self):
        out = prepare_request_body("FORM_URLENCODED", {"a": "b"})
        assert out["data"] == {"a": "b"}

    def test_extra_headers_preservados(self):
        out = prepare_request_body("json", {"x": 1}, extra_headers={"X-Custom": "val"})
        assert out["headers"]["X-Custom"] == "val"
