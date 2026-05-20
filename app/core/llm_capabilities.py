"""Capabilities por provider/model — function calling nativo, etc.

Espelha o pattern de `llm_pricing.py`: dict hardcoded com snapshot de
capacidades atualizado junto com pricing (trimestralmente).

A plataforma usa essas capabilities em `app/agents/engine.py` para
decidir a estratégia de tool execution:

  1. Modelo com `native_tools=True` → bind_tools() do LangChain (OpenAI-compat)
  2. Modelo com `native_tools=False` → fallback prompted_tools (JSON em texto)
  3. Modelo desconhecido → assume False (fallback prompted, conservador)

Isso preserva o **contrato canônico**: o dev declara `Tool Bindings` no
SKILL.md e a plataforma faz funcionar independente do modelo escolhido.
Trocar de azure/gpt-4o para um open-weight sem function calling nativo
deixa de quebrar o agent — apenas degrada para prompted (com audit log).

Modelo desconhecido em runtime → assume False + WARNING. Operador atualiza
o map quando provider novo aparecer.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─── Tabela de capabilities ─────────────────────────────────────
# Chave: f"{provider}/{model}" (case-insensitive, normalizado)
# Valores:
#   native_tools: provider expõe API com `tools` parameter e modelo
#                 retorna structured tool_calls (OpenAI-compat).
#   max_tools:    máximo de tools que o modelo lida bem em 1 chamada.
#                 Modelos pequenos costumam degradar com >10 tools.
#   prompted_ok:  modelo segue instrução estrita o suficiente para
#                 prompted_tools dar resultado confiável (>90% JSON
#                 válido). Modelos minúsculos (3B-) podem falhar.

CAPABILITIES: dict[str, dict] = {
    # Azure OpenAI — tudo nativo, contagem alta de tools
    "azure/gpt-4o":          {"native_tools": True,  "max_tools": 128, "prompted_ok": True},
    "azure/gpt-4o-mini":     {"native_tools": True,  "max_tools": 128, "prompted_ok": True},
    "azure/gpt-4-turbo":     {"native_tools": True,  "max_tools": 128, "prompted_ok": True},
    "azure/gpt-3.5-turbo":   {"native_tools": True,  "max_tools": 64,  "prompted_ok": True},

    # OpenAI direto — alias funcional
    "openai/gpt-4o":         {"native_tools": True,  "max_tools": 128, "prompted_ok": True},
    "openai/gpt-4o-mini":    {"native_tools": True,  "max_tools": 128, "prompted_ok": True},
    "openai/gpt-4-turbo":    {"native_tools": True,  "max_tools": 128, "prompted_ok": True},

    # Anthropic — Tool Use nativo
    "anthropic/claude-opus-4-7":     {"native_tools": True, "max_tools": 64, "prompted_ok": True},
    "anthropic/claude-sonnet-4-6":   {"native_tools": True, "max_tools": 64, "prompted_ok": True},
    "anthropic/claude-haiku-4-5":    {"native_tools": True, "max_tools": 32, "prompted_ok": True},

    # Maritaca — sabia-4 tem function calling; sabia-3 não tem (legacy)
    "maritaca/sabia-4":      {"native_tools": True,  "max_tools": 16, "prompted_ok": True},
    "maritaca/sabia-3":      {"native_tools": False, "max_tools": 8,  "prompted_ok": True},

    # GPT-OSS (open-weight OpenAI 2025) — function calling nativo
    "gpt-oss-20b/openai/gpt-oss-20b":   {"native_tools": True, "max_tools": 32, "prompted_ok": True},
    "gpt-oss-120b/openai/gpt-oss-120b": {"native_tools": True, "max_tools": 64, "prompted_ok": True},
    "gpt-oss-20b/gpt-oss-20b":   {"native_tools": True, "max_tools": 32, "prompted_ok": True},
    "gpt-oss-120b/gpt-oss-120b": {"native_tools": True, "max_tools": 64, "prompted_ok": True},

    # Ollama — varia muito por modelo. Conservador: nativo só quando comprovado.
    "ollama/llama3":         {"native_tools": False, "max_tools": 8,  "prompted_ok": True},
    "ollama/llama3.1":       {"native_tools": True,  "max_tools": 16, "prompted_ok": True},
    "ollama/llama3.2":       {"native_tools": True,  "max_tools": 16, "prompted_ok": True},
    "ollama/gemma":          {"native_tools": False, "max_tools": 4,  "prompted_ok": False},
    "ollama/gemma2":         {"native_tools": False, "max_tools": 8,  "prompted_ok": True},
    "ollama/qwen2.5":        {"native_tools": True,  "max_tools": 16, "prompted_ok": True},
}


def _normalize(provider: Optional[str], model: Optional[str]) -> str:
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    return f"{p}/{m}"


def get_capabilities(provider: Optional[str], model: Optional[str]) -> Optional[dict]:
    """Lookup direto. None se modelo desconhecido."""
    return CAPABILITIES.get(_normalize(provider, model))


def supports_native_tools(provider: Optional[str], model: Optional[str]) -> bool:
    """True se modelo aceita `tools` parameter (function calling nativo).

    Desconhecido → False + WARNING (estratégia conservadora — cai em
    prompted_tools que funciona com qualquer modelo).
    """
    caps = get_capabilities(provider, model)
    if caps is None:
        logger.warning(
            f"llm_capabilities: modelo desconhecido '{_normalize(provider, model)}' — "
            f"assumindo native_tools=False (fallback prompted). Atualize CAPABILITIES "
            f"em app/core/llm_capabilities.py quando confirmar suporte."
        )
        return False
    return bool(caps.get("native_tools", False))


def supports_prompted_tools(provider: Optional[str], model: Optional[str]) -> bool:
    """True se o modelo segue instruções bem o suficiente para prompted_tools.

    Modelos minúsculos (3B-) que falham em JSON estrito têm prompted_ok=False —
    agent cai em modo texto puro (sem tools) com audit warning.
    """
    caps = get_capabilities(provider, model)
    if caps is None:
        return True  # Conservador: tenta prompted mesmo desconhecido.
    return bool(caps.get("prompted_ok", True))


def get_max_tools(provider: Optional[str], model: Optional[str]) -> int:
    """Cap recomendado de tools em uma única chamada. Default 16."""
    caps = get_capabilities(provider, model)
    if caps is None:
        return 16
    return int(caps.get("max_tools", 16))
