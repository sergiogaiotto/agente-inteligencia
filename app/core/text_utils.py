"""Helpers de texto reutilizáveis pelo projeto.

Mantém helpers pequenos e auto-contidos — sem dependências em outros módulos
do `app/` para evitar ciclos. Cada função deve ter um contrato curto e ser
testável isoladamente.
"""

from __future__ import annotations

import re


_SCHEMA_NAME_INVALID_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_SCHEMA_NAME_REPEATED_UNDERSCORE_RE = re.compile(r"_+")


def sanitize_schema_name(
    raw: str | None,
    *,
    fallback: str = "SkillOutput",
    max_len: int = 64,
) -> str:
    """Normaliza um nome livre para ser aceito como `response_format.json_schema.name`.

    A OpenAI exige que o `name` case com `^[a-zA-Z0-9_-]+$` — só alfanuméricos,
    underscore e hífen. Espaços, acentos e pontuação levam o request a 400.

    Fluxo:
    1. Substitui qualquer sequência de caracteres fora do padrão por `_`.
    2. Colapsa underscores repetidos e remove os das pontas.
    3. Trunca em `max_len` (default 64 — limite da OpenAI).
    4. Se sobrar string vazia, usa `fallback`.

    Preserva CamelCase/snake_case quando possível para facilitar debug em logs;
    ao contrário de slugifies genéricos, não força lowercase nem troca por hífen.

    Histórico (2026-06-01): user reportou 400 "Invalid response_format.
    json_schema.name" ao invocar agente _Categorizar Imagem com imagem. O
    SKILL.md tinha `## Output Contract` com `title: "Saida da Categorizar
    Imagem"` — espaços violavam o regex. `engine.py:_build_response_format`
    pegava o title cru.

    Exemplos:
        >>> sanitize_schema_name("Saida da Categorizar Imagem")
        'Saida_da_Categorizar_Imagem'
        >>> sanitize_schema_name("Análise Crédito (PF)")
        'An_lise_Cr_dito_PF'
        >>> sanitize_schema_name("")
        'SkillOutput'
        >>> sanitize_schema_name(None)
        'SkillOutput'
        >>> sanitize_schema_name("x" * 100, max_len=10)
        'xxxxxxxxxx'
    """
    if not raw:
        return fallback
    cleaned = _SCHEMA_NAME_INVALID_CHARS_RE.sub("_", str(raw))
    cleaned = _SCHEMA_NAME_REPEATED_UNDERSCORE_RE.sub("_", cleaned).strip("_")
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def schema_name_is_valid(raw: str | None) -> bool:
    """True se `raw` já é um `response_format.json_schema.name` aceito pela OpenAI.

    Usado por linters/validações para sinalizar quando o operador deveria
    sanitizar antes de salvar (ex.: lint do SKILL.md no `## Output Contract`).
    """
    if not raw:
        return False
    return re.fullmatch(r"[a-zA-Z0-9_-]+", str(raw)) is not None
