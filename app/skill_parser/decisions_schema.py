"""Helper canônico da seção `## Decisions` de uma SKILL.md (Cond-C, 35.18.0).

Espelha `extract_inputs_schema` (## Inputs). Um agente DECLARA as decisões que
anuncia — cada uma um campo com um conjunto FECHADO de valores (enum). A
plataforma injeta (selado) a instrução no prompt para o LLM emitir a linha de
decisão, extrai/valida a saída e expõe `decision.<campo>` no gate condicional.
Isso substitui o contrato OCULTO 'escalar=sim' in output_lower (combinado por
telepatia entre o prompt e a aresta) por um objeto tipado e visível.

Formato canônico:

    ## Decisions
    ```json
    { "escalar": ["sim", "não"], "severidade": ["baixa", "média", "alta"] }
    ```

Política: só stdlib/typing (sem import de app/*) — evita ciclos.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional


def _norm(s: str) -> str:
    """casefold + sem acento — para casar o valor emitido pelo LLM contra o enum
    sem sofrer com maiúscula/acento ('Sim'/'sim'/'SIM' → 'sim')."""
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))
    return s.strip().casefold()


def extract_decisions_schema(skill_md: str) -> Optional[dict]:
    """Extrai o dict {campo: [valores]} da seção `## Decisions`, ou None.

    Rejeita (None) quando: vazio, seção ausente, sem bloco fenced, JSON
    malformado, não-dict, ou nenhum campo VÁLIDO. Um campo é válido quando o
    nome é um identificador (A-Za-z_ seguido de \\w) e os valores são uma lista
    não-vazia de strings distintas (após normalização). Campos inválidos são
    descartados (não derrubam os demais)."""
    if not skill_md:
        return None
    m = re.search(r"##\s+Decisions\s*\n([\s\S]*?)(?=\n##\s|$)", skill_md)
    if not m:
        return None
    fence = re.search(r"```(?:json|JSON)?\s*\n([\s\S]*?)\n```", m.group(1))
    if not fence:
        return None
    try:
        raw = json.loads(fence.group(1).strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for field, values in raw.items():
        if not isinstance(field, str) or not re.match(r"^[A-Za-z_]\w*$", field):
            continue
        if not isinstance(values, list):
            continue
        # valores: strings não-vazias, distintas pela forma normalizada, ordem
        # preservada. Guarda o valor CRU (o que a UI mostra) — a validação da
        # saída do LLM casa pela forma normalizada.
        seen, clean = set(), []
        for v in values:
            if not isinstance(v, str):
                continue
            vs = v.strip()
            if not vs:
                continue
            k = _norm(vs)
            if k in seen:
                continue
            seen.add(k)
            clean.append(vs)
        if clean:
            out[field] = clean
    return out or None


def validate_decision_value(schema: dict, field: str, value: str) -> Optional[str]:
    """Casa `value` (emitido pelo LLM) contra o enum de `field`, tolerante a
    acento/maiúscula. Retorna o valor CANÔNICO (a grafia do schema) ou None se o
    campo não existe / o valor está fora do enum. Assim a expr da aresta compara
    contra a grafia declarada, não contra o que o LLM digitou."""
    allowed = (schema or {}).get(field)
    if not isinstance(allowed, list):
        return None
    target = _norm(value)
    for canonical in allowed:
        if _norm(canonical) == target:
            return canonical
    return None
