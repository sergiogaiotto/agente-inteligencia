"""Testes do crypto helper (app.core.crypto) — encrypt/decrypt secrets at-rest."""

from __future__ import annotations


import pytest


def _reset_fernet_cache():
    """Limpa cache para que testes com env diferente vejam Fernet novo."""
    from app.core import crypto
    crypto._get_fernet.cache_clear()


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    monkeypatch.setenv("MAESTRO_SECRET_KEY", "test-master-key-12345")
    _reset_fernet_cache()
    yield
    _reset_fernet_cache()


class TestEncryptDecrypt:
    def test_roundtrip_basico(self):
        from app.core.crypto import encrypt_secret, decrypt_secret
        out = encrypt_secret("my-api-key-123")
        assert out.startswith("enc::")
        assert decrypt_secret(out) == "my-api-key-123"

    def test_encrypt_vazio_retorna_vazio(self):
        from app.core.crypto import encrypt_secret
        assert encrypt_secret("") == ""
        assert encrypt_secret(None) == ""

    def test_decrypt_vazio_retorna_vazio(self):
        from app.core.crypto import decrypt_secret
        assert decrypt_secret("") == ""

    def test_decrypt_plaintext_legacy_retorna_como_esta(self):
        """Backward compat — valor sem 'enc::' é tratado como plaintext."""
        from app.core.crypto import decrypt_secret
        assert decrypt_secret("plain-legacy-key") == "plain-legacy-key"

    def test_encrypt_idempotente(self):
        """Cifrar valor já cifrado não recifra (evita dupla cifra em update)."""
        from app.core.crypto import encrypt_secret
        out1 = encrypt_secret("my-key")
        out2 = encrypt_secret(out1)
        assert out1 == out2

    def test_is_encrypted_detecta_prefixo(self):
        from app.core.crypto import encrypt_secret, is_encrypted
        e = encrypt_secret("secret")
        assert is_encrypted(e) is True
        assert is_encrypted("plain") is False
        assert is_encrypted("") is False

    def test_decrypt_chave_diferente_retorna_vazio(self, monkeypatch):
        """Se a chave master mudar, decrypt falha silenciosamente (não crash)."""
        from app.core.crypto import encrypt_secret, decrypt_secret
        out = encrypt_secret("my-key")
        # Simula troca de chave
        monkeypatch.setenv("MAESTRO_SECRET_KEY", "outra-chave-diferente")
        _reset_fernet_cache()
        assert decrypt_secret(out) == ""  # token inválido → '' + WARNING

    def test_caracteres_unicode_no_secret(self):
        """Cifrar com chars Unicode deve fazer roundtrip OK."""
        from app.core.crypto import encrypt_secret, decrypt_secret
        original = "sénha-com-acentos-🔑-ção"
        out = encrypt_secret(original)
        assert decrypt_secret(out) == original

    def test_secret_longo(self):
        """Cifrar token longo (3072 chars) sem problemas."""
        from app.core.crypto import encrypt_secret, decrypt_secret
        long_secret = "x" * 3072
        out = encrypt_secret(long_secret)
        assert decrypt_secret(out) == long_secret
