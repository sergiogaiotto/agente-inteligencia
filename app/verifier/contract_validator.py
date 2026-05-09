"""ContractValidator — validador determinístico de output_contract da SKILL.md.

Sem LLM. Roda ANTES do MultiDimJudge para falha precoce em formato.

Suporta 3 modos de detecção do contract:
1) **JSON Schema explícito**: contract começa com `{` e `"type"`/`"properties"` →
   valida com `jsonschema`.
2) **Lista de campos textual**: contract menciona "campos: a, b, c" ou "deve
   conter: a, b, c" → valida que os campos aparecem no draft.
3) **Free text**: nenhuma regra detectável → retorna compliant=True (não pode
   validar deterministicamente).

Tolerante a output em markdown / texto livre / JSON wrapped.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ContractResult:
    compliant: bool
    errors: list[str] = field(default_factory=list)
    parsed_output: Any = None  # JSON parseado se aplicável
    mode: str = "free_text"  # "json_schema" | "field_list" | "free_text"


def validate_contract(draft: str, output_contract: str | None) -> ContractResult:
    """Valida draft contra output_contract.

    Args:
        draft: o output gerado pelo agente
        output_contract: a string do `## Output Contract` da SKILL.md, ou None

    Returns:
        ContractResult com compliant + errors + parsed_output (se JSON) + mode.
    """
    if not output_contract or not output_contract.strip():
        return ContractResult(compliant=True, mode="free_text")
    if not draft:
        return ContractResult(compliant=False, errors=["draft vazio"], mode="unknown")

    contract_str = output_contract.strip()

    # ─── Modo 1: JSON Schema explícito ─────────────────────────
    schema = _extract_json_schema(contract_str)
    if schema is not None:
        parsed = _try_parse_json(draft)
        if parsed is None:
            return ContractResult(
                compliant=False,
                errors=["output_contract pede JSON, mas draft não é JSON válido"],
                mode="json_schema",
            )
        return _validate_json_schema(parsed, schema)

    # ─── Modo 2: Lista de campos textual ───────────────────────
    required_fields = _extract_required_fields(contract_str)
    if required_fields:
        return _validate_field_list(draft, required_fields)

    # ─── Modo 3: Free text — não validável deterministicamente ─
    return ContractResult(compliant=True, mode="free_text")


# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────

def _extract_json_schema(contract_str: str) -> dict | None:
    """Tenta extrair um JSON Schema. Aceita:
    - Contract todo é JSON: `{"type":"object",...}`
    - Contract envolto em ```json ... ```
    - Snippet ```{...}``` em meio a texto
    """
    # Wrapped em fence
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", contract_str, re.DOTALL)
    candidate = fence.group(1) if fence else contract_str

    # Tenta parse direto
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict) and ("type" in obj or "properties" in obj or "required" in obj):
            return obj
    except json.JSONDecodeError:
        pass

    # Procura primeiro `{...}` balanceado
    m = re.search(r"\{.*\}", contract_str, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and ("type" in obj or "properties" in obj or "required" in obj):
                return obj
        except json.JSONDecodeError:
            return None
    return None


def _try_parse_json(draft: str) -> dict | list | None:
    """Tenta parsear draft como JSON. Aceita wrapped em ```."""
    if not draft:
        return None
    candidate = draft.strip()
    # Strip markdown fence
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", candidate)
    if fence:
        candidate = fence.group(1)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Tenta primeiro `{...}` ou `[...]` balanceado
        for pat in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
            m = re.search(pat, candidate)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
    return None


def _validate_json_schema(parsed: Any, schema: dict) -> ContractResult:
    """Valida com `jsonschema` se disponível, senão validação manual de `required`."""
    errors: list[str] = []

    # Lib jsonschema fica como dep opcional — se não estiver instalada, fallback manual
    try:
        from jsonschema import validate as js_validate, ValidationError
        try:
            js_validate(parsed, schema)
            return ContractResult(compliant=True, parsed_output=parsed, mode="json_schema")
        except ValidationError as e:
            errors.append(f"schema violation: {e.message} at {'/'.join(map(str, e.absolute_path))}")
            return ContractResult(compliant=False, errors=errors, parsed_output=parsed, mode="json_schema")
    except ImportError:
        pass

    # Fallback manual: valida apenas `type` e `required`
    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(parsed, dict):
        errors.append(f"esperado objeto, recebido {type(parsed).__name__}")
    if expected_type == "array" and not isinstance(parsed, list):
        errors.append(f"esperado array, recebido {type(parsed).__name__}")
    required = schema.get("required") or []
    if isinstance(parsed, dict):
        for field_name in required:
            if field_name not in parsed:
                errors.append(f"campo obrigatório ausente: {field_name}")
    return ContractResult(
        compliant=not errors,
        errors=errors,
        parsed_output=parsed,
        mode="json_schema",
    )


def _extract_required_fields(contract_str: str) -> list[str]:
    """Heurística para extrair campos obrigatórios de contract textual.

    Padrões reconhecidos:
    - "campos obrigatórios: a, b, c"
    - "campos: a, b, c"
    - "deve conter: a, b, c"
    - "required: a, b, c"
    - "fields: a, b, c"
    """
    patterns = [
        r"campos\s+obrigat[óo]rios?\s*[:=]\s*([a-zA-Z0-9_,\s]+)",
        r"deve\s+conter\s*[:=]\s*([a-zA-Z0-9_,\s]+)",
        r"\brequired\s*[:=]\s*([a-zA-Z0-9_,\s]+)",
        r"\bfields\s*[:=]\s*([a-zA-Z0-9_,\s]+)",
        r"\bcampos\s*[:=]\s*([a-zA-Z0-9_,\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, contract_str, re.IGNORECASE)
        if m:
            raw = m.group(1)
            # Para no primeiro `\n\n` ou `.\s` para evitar engolir o resto do texto
            raw = re.split(r"\n\n|\.\s", raw, maxsplit=1)[0]
            fields = [f.strip().strip('"\'`') for f in raw.split(",")]
            return [f for f in fields if f and len(f) <= 64]
    return []


def _validate_field_list(draft: str, required_fields: list[str]) -> ContractResult:
    """Verifica que cada campo aparece no draft (case-insensitive substring).

    Heurística simples: se for JSON, parse e checa keys. Senão checa substring.
    """
    parsed = _try_parse_json(draft)
    errors: list[str] = []
    if isinstance(parsed, dict):
        for f in required_fields:
            if f not in parsed:
                errors.append(f"campo obrigatório ausente: {f}")
        return ContractResult(
            compliant=not errors,
            errors=errors,
            parsed_output=parsed,
            mode="field_list",
        )
    # Texto livre — checa substring case-insensitive
    draft_low = draft.lower()
    for f in required_fields:
        if f.lower() not in draft_low:
            errors.append(f"menção ao campo '{f}' não encontrada no output")
    return ContractResult(
        compliant=not errors,
        errors=errors,
        mode="field_list",
    )
