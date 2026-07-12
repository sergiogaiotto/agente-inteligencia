"""Pricing de LLM EDITÁVEL em runtime (TCO auditável).

A tabela de preços deixa de ser só um snapshot hardcoded: overrides vivem em
platform_settings e são aplicados no boot (apply_settings_to_env) — o operador
edita na tela (Configurações → Preços LLM) sem deploy. get_pricing consulta o
override ANTES do default; effective_pricing() alimenta a UI marcando a origem.
"""
from pathlib import Path

import pytest

from app.core import llm_pricing


@pytest.fixture(autouse=True)
def _reset_overrides():
    llm_pricing.set_pricing_overrides({})
    yield
    llm_pricing.set_pricing_overrides({})


def test_override_precede_o_default_no_compute_cost():
    # default azure/gpt-4o = 0.0025 in / 0.01 out
    base = llm_pricing.compute_cost("azure", "gpt-4o", 1000, 1000)
    assert base == pytest.approx(0.0025 + 0.01)
    llm_pricing.set_pricing_overrides({"azure/gpt-4o": {"input": 0.003, "output": 0.012}})
    assert llm_pricing.get_pricing("azure", "gpt-4o") == {"input": 0.003, "output": 0.012}
    assert llm_pricing.compute_cost("azure", "gpt-4o", 1000, 1000) == pytest.approx(0.003 + 0.012)


def test_override_habilita_modelo_desconhecido():
    # sem override, modelo desconhecido → 0 (com warning)
    assert llm_pricing.compute_cost("acme", "novo-llm", 1000, 0) == 0.0
    llm_pricing.set_pricing_overrides({"acme/novo-llm": {"input": 0.02, "output": 0.04}})
    assert llm_pricing.compute_cost("acme", "novo-llm", 1000, 1000) == pytest.approx(0.02 + 0.04)


def test_set_overrides_ignora_invalidos():
    n = llm_pricing.set_pricing_overrides({
        "sem-barra": {"input": 1, "output": 2},        # chave inválida (sem /)
        "azure/neg": {"input": -1, "output": 2},       # negativo
        "azure/faltando": {"input": 1},                # falta output
        "azure/ok": {"input": 0.001, "output": 0.002},  # válido
    })
    assert n == 1
    assert "azure/ok" in llm_pricing.get_pricing_overrides()


def test_effective_pricing_marca_origem():
    llm_pricing.set_pricing_overrides({"azure/gpt-4o": {"input": 9, "output": 9}})
    eff = {r["key"]: r for r in llm_pricing.effective_pricing()}
    assert eff["azure/gpt-4o"]["overridden"] is True
    assert eff["azure/gpt-4o"]["default_input"] == 0.0025      # default preservado p/ restaurar
    assert eff["openai/gpt-4o"]["overridden"] is False


def test_boot_load_no_apply_settings():
    """apply_settings_to_env carrega os overrides do banco na camada runtime."""
    src = Path("app/core/config.py").read_text(encoding="utf-8")
    assert "llm_pricing_overrides" in src
    assert "set_pricing_overrides" in src


def test_endpoints_de_pricing_registrados():
    src = Path("app/routes/dashboard.py").read_text(encoding="utf-8")
    assert '@router.get("/settings/pricing")' in src
    assert '@router.put("/settings/pricing")' in src
    assert "effective_pricing" in src and "set_pricing_overrides" in src
    assert 'settings_store.set("llm_pricing_overrides"' in src
    assert 'require_role("root", "admin")' in src          # gate de role


def test_ui_de_precos_no_settings():
    src = Path("app/templates/pages/settings.html").read_text(encoding="utf-8")
    assert 'data-testid="settings-tab-pricing"' in src
    assert 'data-testid="settings-pricing-tab"' in src
    assert "loadPricing()" in src and "savePricing()" in src and "_pricingOverrides()" in src
    assert 'data-testid="pricing-add"' in src and "addPricingModel()" in src
    assert "api.put('/api/v1/settings/pricing'" in src and "api.get('/api/v1/settings/pricing')" in src
