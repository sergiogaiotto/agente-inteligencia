"""Pricing por provider/model — USD por 1k tokens (Onda 4 PR #69).

Substitui o `cost_usd=0` placeholder do executor de recipes (PR #67) por
cálculo real baseado em tokens de input/output × preço do modelo do agent.

**Decisão de escopo**: pricing é hardcoded em Python (vs tabela DB editável).
Reasoning: mudanças de preço de LLM são raras (trimestrais) e devem passar
por revisão de código. Se o ritmo aumentar, migra-se para uma tabela DB
num PR futuro sem mudar a API de `compute_cost`.

**Granularidade**: input e output têm preços diferentes (output costuma ser
3-5x mais caro). Engine retorna `tokens.input` e `tokens.output` separados
([app/agents/engine.py:1165](app/agents/engine.py:1165)), e o executor passa
ambos para `compute_cost`.

Modelo desconhecido → custo 0 + warning log. Não derruba o fluxo — operador
pode atualizar `PRICING` depois e re-rodar. Tokens nunca devem ficar sem
registro só porque um modelo novo apareceu.

Preços: snapshot em 2026-05 das tabelas públicas dos fornecedores. Manter
sincronizado quando provider anunciar mudança.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Tabela de preços (USD por 1.000 tokens) ─────────────────────
# Chave: f"{provider}/{model}" (case-insensitive, normalizado em compute_cost)
# Valores: {"input": <USD/1k>, "output": <USD/1k>}

PRICING: dict[str, dict[str, float]] = {
    # Azure OpenAI — preços por região variam ±10%; usamos US East 2 como referência
    "azure/gpt-4o":          {"input": 0.0025, "output": 0.01},
    "azure/gpt-4o-mini":     {"input": 0.00015, "output": 0.0006},
    "azure/gpt-4-turbo":     {"input": 0.01, "output": 0.03},
    "azure/gpt-3.5-turbo":   {"input": 0.0005, "output": 0.0015},

    # OpenAI direto — alias de azure (mesmos preços base; região US)
    "openai/gpt-4o":         {"input": 0.0025, "output": 0.01},
    "openai/gpt-4o-mini":    {"input": 0.00015, "output": 0.0006},
    "openai/gpt-4-turbo":    {"input": 0.01, "output": 0.03},

    # Anthropic — preços públicos 2026-05
    "anthropic/claude-opus-4-7":      {"input": 0.015, "output": 0.075},
    "anthropic/claude-sonnet-4-6":    {"input": 0.003, "output": 0.015},
    "anthropic/claude-haiku-4-5":     {"input": 0.0008, "output": 0.004},

    # Maritaca (Brasil) — Sabiá é mais barato para PT-BR
    "maritaca/sabia-4":      {"input": 0.0005, "output": 0.0015},
    "maritaca/sabia-3":      {"input": 0.0003, "output": 0.001},

    # Ollama (self-hosted) — sem custo de token, mas mantemos a entrada
    # explícita para evitar warning de "modelo desconhecido"
    "ollama/llama3":         {"input": 0.0, "output": 0.0},
    "ollama/llama3.1":       {"input": 0.0, "output": 0.0},
    "ollama/gemma":          {"input": 0.0, "output": 0.0},
    "ollama/gemma2":         {"input": 0.0, "output": 0.0},
    "ollama/qwen2.5":        {"input": 0.0, "output": 0.0},
}


def _normalize(provider: Optional[str], model: Optional[str]) -> str:
    """Constrói a chave normalizada. None/empty viram '' — sempre faz miss."""
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    return f"{p}/{m}"


def get_pricing(provider: Optional[str], model: Optional[str]) -> Optional[dict[str, float]]:
    """Lookup direto na tabela. None se desconhecido (caller decide o fallback)."""
    return PRICING.get(_normalize(provider, model))


def compute_cost(
    provider: Optional[str],
    model: Optional[str],
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Calcula custo em USD para uma invocação.

    Args:
        provider: ex 'azure', 'anthropic', 'maritaca', 'ollama'
        model: ex 'gpt-4o', 'claude-sonnet-4-6', 'sabia-4'
        input_tokens: tokens de prompt
        output_tokens: tokens gerados pelo modelo

    Returns:
        Custo em USD (float). Modelo desconhecido → 0 + warning.
    """
    if input_tokens < 0 or output_tokens < 0:
        # Defensivo: tokens negativos não fazem sentido — trate como 0
        input_tokens = max(0, input_tokens)
        output_tokens = max(0, output_tokens)

    pricing = get_pricing(provider, model)
    if pricing is None:
        logger.warning(
            f"llm_pricing: modelo desconhecido '{_normalize(provider, model)}' — "
            f"cost_usd=0 (atualize PRICING em app/core/llm_pricing.py)"
        )
        return 0.0

    cost = (
        (input_tokens / 1000.0) * pricing["input"]
        + (output_tokens / 1000.0) * pricing["output"]
    )
    return round(cost, 6)
