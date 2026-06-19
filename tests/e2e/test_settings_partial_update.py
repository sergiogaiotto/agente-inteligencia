"""Regressão: salvar um SUBSET de configurações NÃO pode zerar as outras chaves.

Incidente desta sessão: salvar a aba Plataforma (que envia só os campos dela)
caía no default "" dos demais campos do SettingsSave e ZERAVA segredos de outras
abas (azure_key, URLs do gpt-oss, primary_provider…). Mudar o fuso apagava a
config de LLM. Fix: PUT /api/v1/settings usa model_dump(exclude_unset=True) —
persiste só o que foi explicitamente enviado.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _get(api, key):
    d = api.get("/api/v1/settings").json()
    s = d.get("settings", d) if isinstance(d, dict) else {}
    return s.get(key)


def test_save_parcial_nao_zera_outras_chaves(api):
    sentinel = "https://sentinela-e2e.example/v1"
    original = _get(api, "oss20b_url")
    try:
        # grava uma sentinela numa chave de outra "aba"
        r = api.put("/api/v1/settings", json={"oss20b_url": sentinel})
        assert r.status_code in (200, 201), r.text
        assert _get(api, "oss20b_url") == sentinel

        # salva OUTRA chave (subset, como faz a aba Plataforma) — NÃO pode zerar a sentinela
        r2 = api.put("/api/v1/settings", json={"timezone": "America/Sao_Paulo"})
        assert r2.status_code in (200, 201), r2.text
        assert _get(api, "oss20b_url") == sentinel, "save parcial ZEROU outra chave (footgun)!"
    finally:
        # restaura o valor original da chave usada como sentinela
        if original:
            try:
                api.put("/api/v1/settings", json={"oss20b_url": original})
            except Exception:
                pass
