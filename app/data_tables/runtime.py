"""Helpers de runtime da Onda Tabular — flags de comportamento lidas do env.

Fonte ÚNICA para rotas, parser e engine checarem se o Tier 2 (text-to-SQL
governado / "Perguntar à Tabela") está ligado, evitando drift na lógica de
parsing do env entre módulos. Espelha `app/mcp/runtime.py::per_tool_enabled`.
"""

from __future__ import annotations

import os


def text_to_sql_enabled() -> bool:
    """Flag do Tier 2 — text-to-SQL governado (bancada "Perguntar à Tabela").

    Default OFF → nenhum endpoint/aba do Tier 2 existe ou aparece; o Tier 1
    parametrizado segue idêntico. Lê o env a CADA chamada (testável via
    monkeypatch; sem cache de processo), de modo que o toggle de Configurações
    vale em runtime, sem restart — paridade com `per_tool_enabled()`.

    Mapeado de `text_to_sql_enabled` (platform_settings) → `TEXT_TO_SQL_ENABLED`
    por `app.core.config.apply_settings_to_env`.
    """
    return os.getenv("TEXT_TO_SQL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
