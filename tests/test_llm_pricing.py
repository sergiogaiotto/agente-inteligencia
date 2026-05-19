"""Testes do módulo de pricing por provider/model (Onda 4 PR #69)."""

from __future__ import annotations

import logging

import pytest

from app.core.llm_pricing import PRICING, compute_cost, get_pricing


class TestGetPricing:
    def test_modelo_conhecido(self):
        p = get_pricing("azure", "gpt-4o")
        assert p is not None
        assert p["input"] == 0.0025
        assert p["output"] == 0.01

    def test_normaliza_case(self):
        # Lookup tolerante a caixa
        assert get_pricing("Azure", "GPT-4o") == get_pricing("azure", "gpt-4o")
        assert get_pricing("  azure  ", "gpt-4o") == get_pricing("azure", "gpt-4o")

    def test_modelo_desconhecido_retorna_none(self):
        assert get_pricing("foo", "bar") is None

    def test_none_inputs(self):
        # None ou empty → não acha
        assert get_pricing(None, None) is None
        assert get_pricing("azure", None) is None
        assert get_pricing(None, "gpt-4o") is None


class TestComputeCost:
    def test_calculo_basico(self):
        # azure/gpt-4o: 1000 in × 0.0025/1k = 0.0025; 500 out × 0.01/1k = 0.005
        # total = 0.0075
        assert compute_cost("azure", "gpt-4o", 1000, 500) == 0.0075

    def test_zero_tokens(self):
        assert compute_cost("azure", "gpt-4o", 0, 0) == 0.0

    def test_apenas_input(self):
        # 2000 input × 0.0025/1k = 0.005
        assert compute_cost("azure", "gpt-4o", 2000, 0) == 0.005

    def test_apenas_output(self):
        # 1000 output × 0.01/1k = 0.01
        assert compute_cost("azure", "gpt-4o", 0, 1000) == 0.01

    def test_modelo_desconhecido_retorna_zero(self, caplog):
        with caplog.at_level(logging.WARNING):
            cost = compute_cost("foo", "bar", 1000, 500)
        assert cost == 0.0
        # E deixa warning para operador adicionar pricing depois
        assert any("modelo desconhecido" in r.message for r in caplog.records)

    def test_tokens_negativos_tratados_como_zero(self):
        # Defensivo: tokens negativos não fazem sentido
        assert compute_cost("azure", "gpt-4o", -100, -50) == 0.0

    def test_ollama_zero_cost(self):
        # Ollama é self-hosted; preços=0 mas a entrada existe (sem warning)
        cost = compute_cost("ollama", "llama3", 5000, 3000)
        assert cost == 0.0

    def test_diferenciacao_input_output(self):
        # Output é mais caro — verificar que a diferença reflete na chamada
        same_in = compute_cost("azure", "gpt-4o", 1000, 0)
        same_out = compute_cost("azure", "gpt-4o", 0, 1000)
        assert same_out > same_in  # output_per_1k > input_per_1k

    def test_arredondamento_6_casas(self):
        # 1 token × pricing — número minúsculo deve manter 6 casas
        cost = compute_cost("azure", "gpt-4o-mini", 1, 0)
        # 1/1000 × 0.00015 = 0.00000015 → 0.0 após arredondar a 6 casas
        # garantia: não vira NaN nem explode
        assert isinstance(cost, float)
        assert cost >= 0.0

    def test_maritaca_sabia(self):
        # Provider brasileiro — deve estar na tabela
        cost = compute_cost("maritaca", "sabia-4", 10000, 2000)
        # 10k × 0.0005/1k + 2k × 0.0015/1k = 0.005 + 0.003 = 0.008
        assert cost == 0.008

    def test_anthropic_claude_opus(self):
        # Modelo mais caro — sanity check
        cost = compute_cost("anthropic", "claude-opus-4-7", 1000, 500)
        # 1k × 0.015/1k + 0.5k × 0.075/1k = 0.015 + 0.0375 = 0.0525
        assert cost == 0.0525


class TestPricingTable:
    def test_estrutura_consistente(self):
        # Toda entrada deve ter input + output
        for key, value in PRICING.items():
            assert "input" in value, f"{key} sem 'input'"
            assert "output" in value, f"{key} sem 'output'"
            assert isinstance(value["input"], (int, float))
            assert isinstance(value["output"], (int, float))
            assert value["input"] >= 0
            assert value["output"] >= 0

    def test_output_costuma_ser_maior_que_input(self):
        # Convenção: providers cobram output mais caro (exceto ollama=0/0)
        # Sinaliza erros de digitação na tabela
        for key, value in PRICING.items():
            if value["input"] == 0 and value["output"] == 0:
                continue  # ollama
            assert value["output"] >= value["input"], (
                f"{key}: output ({value['output']}) < input ({value['input']}) — "
                f"erro de digitação?"
            )
