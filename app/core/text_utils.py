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


def coerce_to_openai_strict_schema(schema):
    """Adapta um JSON Schema arbitrário ao subset que a OpenAI aceita em
    `response_format.json_schema` com `strict: true`.

    Regras do strict mode (https://platform.openai.com/docs/guides/structured-outputs):
    - Todo `type: "object"` com `properties` precisa de `required` listando
      TODAS as keys de `properties` (não há campos opcionais em strict mode).
    - Todo `type: "object"` precisa de `additionalProperties: false`.
    - Aplica-se recursivamente em propriedades aninhadas, `items` de arrays e
      branches de `oneOf`/`anyOf`/`allOf`.

    Histórico (2026-06-01): após PR #248 ter sanitizado o `name`, surgiu o
    erro "'required' is required to be supplied and to be an array including
    every key in properties" — a skill do user declarava `required` parcial
    (subset de `properties`). Strict mode rejeita.

    Preserva o objeto original (não muta). Devolve uma cópia profunda dos
    nós alterados. Não-dict (None, listas em raiz, strings) volta intacto.

    Exemplos:
        >>> coerce_to_openai_strict_schema({
        ...     "type": "object",
        ...     "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        ...     "required": ["a"],
        ... })
        {'type': 'object', 'properties': {'a': {'type': 'string'}, 'b': {'type': 'integer'}}, 'required': ['a', 'b'], 'additionalProperties': False}
    """
    if not isinstance(schema, dict):
        return schema

    out = {}
    for k, v in schema.items():
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: coerce_to_openai_strict_schema(pv) for pk, pv in v.items()}
        elif k == "items":
            # Pode ser dict (schema único) ou lista (tuple-typed arrays)
            if isinstance(v, list):
                out[k] = [coerce_to_openai_strict_schema(it) for it in v]
            else:
                out[k] = coerce_to_openai_strict_schema(v)
        elif k in ("oneOf", "anyOf", "allOf") and isinstance(v, list):
            out[k] = [coerce_to_openai_strict_schema(it) for it in v]
        elif k in ("not", "if", "then", "else") and isinstance(v, dict):
            out[k] = coerce_to_openai_strict_schema(v)
        elif k == "definitions" and isinstance(v, dict):
            # Algumas skills usam $ref + definitions — coerce cada definition.
            out[k] = {dk: coerce_to_openai_strict_schema(dv) for dk, dv in v.items()}
        elif k == "$defs" and isinstance(v, dict):
            out[k] = {dk: coerce_to_openai_strict_schema(dv) for dk, dv in v.items()}
        else:
            out[k] = v

    # Strict mode: object → required = todas as keys + additionalProperties=false.
    # Aplicamos só quando há `properties` para evitar mexer em objetos abertos
    # legítimos (ex.: type=object sem schema fechado).
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["required"] = list(out["properties"].keys())
        out["additionalProperties"] = False

    return out
