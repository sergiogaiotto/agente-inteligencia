"""Hermeticidade da suíte (33.2.1) — o conftest raiz carrega .env.test.

Prova que a suíte roda em ambiente DETERMINÍSTICO (APP_ENV=test + MAESTRO/SECRET
fixos) independente do .env real de dev, e que o crypto NÃO levanta (o modo
'produção' do .env local fazia ~7 testes de federação falharem sem o workaround
APP_ENV=development). Ver tests/conftest.py::_load_env_test.
"""

from __future__ import annotations

import os


def test_env_test_carregado_no_ambiente():
    # O conftest força estes valores em os.environ no import (antes de qualquer
    # import de app.core.*).
    assert os.environ.get("APP_ENV") == "test"
    assert os.environ.get("MAESTRO_SECRET_KEY")   # não-vazio
    assert os.environ.get("SECRET_KEY")           # não-vazio


def test_nao_e_producao():
    # is_production() deve ser False → guards de prod no-op, crypto no caminho
    # normal (sem o fail-fast que quebrava os testes de federação local).
    from app.core.config import is_production
    assert is_production() is False


def test_crypto_roundtrip_sem_raise_de_producao():
    # Com MAESTRO em os.environ, _get_fernet constrói a cifra real e faz o
    # roundtrip — sem o RuntimeError('MAESTRO ausente em produção') que
    # derrubava federation_peers/egress no .env local.
    from app.core.crypto import encrypt_secret, decrypt_secret

    enc = encrypt_secret("segredo-de-teste")
    assert enc.startswith("enc::")
    assert decrypt_secret(enc) == "segredo-de-teste"


def test_get_settings_le_app_env_test():
    from app.core.config import get_settings
    assert get_settings().app_env == "test"
