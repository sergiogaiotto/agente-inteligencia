"""SEC-02 — boot guard de produção.

Falha-fecha o boot quando ``app_env`` é produção com default inseguro
(SECRET_KEY 'change-me', MAESTRO_SECRET_KEY ausente, COOKIE_SECURE=false).
No-op em dev/staging — os defaults convenientes continuam bootando.

Cobre app/core/config.assert_secure_production_posture + is_production e o
fail-fast defensivo de app/core/crypto._get_fernet.
"""

from __future__ import annotations

import pytest

from app.core.config import (
    InsecureProductionConfigError,
    Settings,
    assert_secure_production_posture,
    is_production,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Isola o lru_cache de get_settings/_get_fernet — evita vazar prod p/ outros testes."""
    from app.core import config, crypto

    config.get_settings.cache_clear()
    crypto._get_fernet.cache_clear()
    yield
    config.get_settings.cache_clear()
    crypto._get_fernet.cache_clear()


def _settings(**overrides) -> Settings:
    """Settings com postura SEGURA por default; cada teste subverte um eixo.

    Kwargs de init vencem .env/os.environ (maior precedência do pydantic-settings),
    então o teste é determinístico independente do .env local.
    """
    base = dict(
        app_env="production",
        secret_key="uma-chave-forte-e-aleatoria-xyz",
        cookie_secure=True,
    )
    base.update(overrides)
    return Settings(**base)


class TestIsProduction:
    def test_variantes_de_producao(self):
        assert is_production(_settings(app_env="production")) is True
        assert is_production(_settings(app_env="PROD")) is True
        assert is_production(_settings(app_env=" Production ")) is True

    def test_nao_producao(self):
        assert is_production(_settings(app_env="development")) is False
        assert is_production(_settings(app_env="staging")) is False
        assert is_production(_settings(app_env="test")) is False


class TestBootGuard:
    def test_prod_seguro_passa(self, monkeypatch):
        monkeypatch.setenv("MAESTRO_SECRET_KEY", "master-key-real")
        # Não lança.
        assert assert_secure_production_posture(_settings()) is None

    def test_dev_com_defaults_inseguros_ainda_boota(self, monkeypatch):
        """O eixo CRÍTICO: dev com TODOS os defaults inseguros é no-op."""
        monkeypatch.delenv("MAESTRO_SECRET_KEY", raising=False)
        s = _settings(app_env="development", secret_key="change-me", cookie_secure=False)
        assert assert_secure_production_posture(s) is None

    def test_prod_secret_key_default_barra(self, monkeypatch):
        monkeypatch.setenv("MAESTRO_SECRET_KEY", "master-key-real")
        with pytest.raises(InsecureProductionConfigError) as ei:
            assert_secure_production_posture(_settings(secret_key="change-me"))
        assert "SECRET_KEY" in str(ei.value)

    def test_prod_secret_key_em_branco_barra(self, monkeypatch):
        monkeypatch.setenv("MAESTRO_SECRET_KEY", "master-key-real")
        with pytest.raises(InsecureProductionConfigError):
            assert_secure_production_posture(_settings(secret_key="   "))

    def test_prod_sem_maestro_key_barra(self, monkeypatch):
        monkeypatch.delenv("MAESTRO_SECRET_KEY", raising=False)
        with pytest.raises(InsecureProductionConfigError) as ei:
            assert_secure_production_posture(_settings())
        assert "MAESTRO_SECRET_KEY" in str(ei.value)

    def test_prod_cookie_inseguro_avisa_mas_nao_barra(self, monkeypatch, caplog):
        """cookie_secure=False (com o resto seguro) NÃO bloqueia — só loga WARNING.

        Bloquear quebraria o debug local por http sob app_env=production.
        """
        import logging

        monkeypatch.setenv("MAESTRO_SECRET_KEY", "master-key-real")
        with caplog.at_level(logging.WARNING, logger="app.core.config"):
            assert assert_secure_production_posture(_settings(cookie_secure=False)) is None
        assert any("cookie_insecure_prod" in r.getMessage() for r in caplog.records)

    def test_hard_fails_reportados_juntos(self, monkeypatch):
        """SECRET_KEY + MAESTRO ausentes → uma exceção só (conserto num restart)."""
        monkeypatch.delenv("MAESTRO_SECRET_KEY", raising=False)
        with pytest.raises(InsecureProductionConfigError) as ei:
            assert_secure_production_posture(
                _settings(secret_key="change-me", cookie_secure=False)
            )
        msg = str(ei.value)
        assert "SECRET_KEY" in msg
        assert "MAESTRO_SECRET_KEY" in msg


class TestCryptoFailFast:
    def test_crypto_barra_em_prod_sem_maestro(self, monkeypatch):
        """Defesa em profundidade: _get_fernet lança em prod mesmo se o guard for contornado."""
        from app.core import config, crypto

        monkeypatch.delenv("MAESTRO_SECRET_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        config.get_settings.cache_clear()
        crypto._get_fernet.cache_clear()
        with pytest.raises(RuntimeError):
            crypto._get_fernet()

    def test_crypto_fallback_em_dev(self, monkeypatch):
        """Em dev, sem MAESTRO_SECRET_KEY, mantém o fallback (não lança)."""
        from app.core import config, crypto

        monkeypatch.delenv("MAESTRO_SECRET_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "development")
        config.get_settings.cache_clear()
        crypto._get_fernet.cache_clear()
        fernet = crypto._get_fernet()
        assert fernet is not None
        # roundtrip continua funcionando em dev
        token = fernet.encrypt(b"segredo")
        assert fernet.decrypt(token) == b"segredo"
