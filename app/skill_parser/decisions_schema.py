"""Helper canônico da seção `## Decisions` de uma SKILL.md (Cond-C, 35.19.0).

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

Linha emitida pelo LLM (última linha da resposta):

    DECISAO: escalar=sim; severidade=alta

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


# Linha de decisão na saída do LLM. Aceita "DECISAO"/"DECISÃO"/"Decisao" (o
# modelo pt-BR alterna acento/caixa) e um prefixo leve de markdown (**, ##, >,
# -) que modelos costumam pôr na última linha. O corpo é "campo=valor" separado
# por ';' (ou ','). MULTILINE: pega a linha onde quer que esteja — a validação
# contra o schema é quem sela (campo/valor fora do contrato são descartados).
_DECISION_LINE_RE = re.compile(
    r"^[\s>*#-]*DECIS[AÃ]O\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_DECISION_PAIR_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*([^;,]+)")


def has_decision_line(text: str) -> bool:
    """Cheque BARATO (sem schema): o texto contém uma linha `DECISAO: ...`?
    Usado pelo gate para só pagar o lookup de skill quando há o que validar."""
    return bool(text) and bool(_DECISION_LINE_RE.search(text))


def extract_decision_line(text: str, schema: Optional[dict]) -> dict:
    """Extrai e VALIDA a(s) linha(s) `DECISAO: campo=valor; ...` de `text`.

    Retorna {campo: valor_CANÔNICO} contendo SÓ campos declarados no schema com
    valor dentro do enum (tolerante a acento/maiúscula — devolve a grafia do
    schema). Pares fora do contrato são descartados em silêncio: o contrato é
    SELADO, o LLM não inventa campo nem valor. Linhas posteriores sobrescrevem
    anteriores por campo (a última palavra do modelo vence — cobre o eco do
    formato no meio da resposta). Sem linha / sem schema / nada válido → {}.
    """
    if not text or not schema:
        return {}
    out: dict = {}
    for body in _DECISION_LINE_RE.findall(text):
        for field, value in _DECISION_PAIR_RE.findall(body):
            # bordas: aspas, crase e o markdown que sobra do fecho da linha
            # (`**Decisão: x=Alta**` → valor capturado 'Alta**') + ponto final.
            cleaned = value.strip().strip("\"'`*_.").strip()
            canonical = validate_decision_value(schema, field, cleaned)
            if canonical is not None:
                out[field] = canonical
    return out


def build_decisions_directive(schema: dict) -> str:
    """Bloco pt-BR SELADO pela plataforma, anexado ao system prompt quando a
    skill declara `## Decisions`. Instrui o LLM a fechar a resposta com a linha
    `DECISAO:` usando SÓ os campos/valores do contrato — é essa linha que o
    gate condicional (`decision.<campo>`) lê para rotear o fluxo."""
    fields = "\n".join(
        f"- {field}: " + " | ".join(values) for field, values in schema.items()
    )
    # Shape com <a|b> (não um valor concreto) — um exemplo real enviesaria o
    # modelo a copiar o 1º valor do enum em vez de decidir.
    shape = "; ".join(
        f"{field}=<" + "|".join(values) + ">" for field, values in schema.items()
    )
    return (
        "\n## Contrato de Decisão (selado pela plataforma)\n"
        "Encerre SEMPRE sua resposta com UMA linha exata no formato:\n"
        f"DECISAO: {shape}\n"
        "Anuncie TODOS os campos abaixo, escolhendo APENAS um dos valores "
        "permitidos para cada um (não invente campos nem valores; não explique "
        "a linha — ela é lida pela plataforma para decidir o próximo passo do "
        "fluxo):\n"
        f"{fields}"
    )


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
