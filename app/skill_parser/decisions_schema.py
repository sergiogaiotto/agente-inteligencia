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


# Campos proibidos no contrato: atributos/métodos de dict — `decision.<campo>`
# no Jinja resolveria o MÉTODO em vez do valor anunciado (regra sempre-falsa).
_RESERVED_FIELDS = frozenset(dir(dict))


def extract_decisions_schema(skill_md: str) -> Optional[dict]:
    """Extrai o dict {campo: [valores]} da seção `## Decisions`, ou None.

    Rejeita (None) quando: vazio, seção ausente, sem bloco fenced, JSON
    malformado, não-dict, ou nenhum campo VÁLIDO. Um campo é válido quando o
    nome é um identificador ASCII (A-Za-z_ seguido de A-Za-z0-9_) e os valores
    são uma lista não-vazia de strings distintas (após normalização) SEM os
    separadores da linha (';', ',', '='). Campos inválidos são descartados
    (não derrubam os demais)."""
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
        # ASCII puro (não \w, que em Python 3 é unicode e aceitaria 'situação'):
        # o lookup do parser é EXATO e a expr da aresta precisa digitar o campo —
        # um campo acentuado nasceria com falso-negativo garantido (review 2026-07-15).
        if not isinstance(field, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", field):
            continue
        # Nomes de atributo/método de dict ('items', 'get', 'values'...): o gate
        # expõe `decision` como dict e o Jinja resolve ATRIBUTO antes de item —
        # `decision.items` devolveria o método, e a regra nunca casaria
        # (review pré-push 2026-07-15).
        if field in _RESERVED_FIELDS:
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
            # ';' ',' '=' são separadores da linha DECISAO — um valor que os
            # contém é IRREPRESENTÁVEL (`_DECISION_PAIR_RE` para no 1º ';'/',')
            # e o contrato nasceria morto sem aviso (review 2026-07-15).
            if any(ch in vs for ch in ";,="):
                continue
            # Idem p/ bordas que `extract_decision_line` STRIPA do valor emitido
            # ("'`*_.): o LLM emite verbatim, a extração remove o char, o match
            # contra o enum falha para sempre (review pré-push do Cond-C.2).
            if vs != vs.strip("\"'`*_."):
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
# modelo pt-BR alterna acento/caixa), os aliases traduzidos "DECISION"/"DECISIÓN"
# (defesa em profundidade: agente com response_language en-US/es pode traduzir o
# prefixo apesar da diretiva verbatim — review 2026-07-15; o ENUM continua selando
# os valores, então o alias não abre brecha) e um prefixo leve de markdown
# (**, ##, >, -) que modelos costumam pôr na última linha. O corpo é
# "campo=valor" separado por ';' (ou ','). MULTILINE: pega a linha onde quer que
# esteja — a validação contra o schema é quem sela (campo/valor fora do contrato
# são descartados).
_DECISION_LINE_RE = re.compile(
    r"^[\s>*#-]*DECIS(?:[AÃ]O|ION|IÓN)\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
# Grafias que a linha pode assumir — usadas p/ reconhecer um PREFIXO cortado pelo
# truncate ("...DECIS…" sem ':') em preserve_decision_line.
_DECISION_KEYWORDS = ("DECISAO", "DECISÃO", "DECISION", "DECISIÓN")
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
        "fluxo). A linha é um token técnico lido por MÁQUINA: copie a palavra "
        "DECISAO e os valores EXATAMENTE como listados, SEM traduzir nem "
        "adaptar ao idioma da resposta:\n"
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


def build_decision_line(decision: dict) -> str:
    """Forma CANÔNICA da linha (o inverso de `extract_decision_line`): sempre
    parseável de volta. Usada p/ re-anexar a decisão ao draft truncado."""
    return "DECISAO: " + "; ".join(f"{k}={v}" for k, v in decision.items())


def _is_cut_decision_prefix(line: str) -> bool:
    """True quando `line` parece um PREFIXO da linha DECISAO cortado pelo
    truncate ANTES do ':' (ex.: '**DECIS…'). Com ':' presente, o corte parcial
    já casa `_DECISION_LINE_RE` e é tratado pelo caminho normal. Critério
    estrito — o corpo (sem markdown/reticências) precisa ser prefixo de uma
    das grafias aceitas — para NUNCA descartar prosa legítima ('Decisões…'
    não é prefixo de nenhuma grafia)."""
    body = line.strip().lstrip(">*# -\t").rstrip("…").strip()
    if not body:
        return False
    up = body.upper()
    return any(kw.startswith(up) for kw in _DECISION_KEYWORDS)


def preserve_decision_line(original: str, truncated: str, schema: Optional[dict]) -> str:
    """Pós-truncate do Output Shape: garante que a decisão do draft ORIGINAL
    sobrevive INTEIRA no texto truncado.

    O guard antigo (`has_decision_line(truncated)`) falhava no caso MAIS
    provável: a linha é a ÚLTIMA por contrato, então um overflow pequeno corta
    NO MEIO dela — `has_decision_line` ainda casava e campos após o corte
    sumiam em silêncio ('severidade=al…' morre no enum) (review 2026-07-15).
    Compara a EXTRAÇÃO validada do original vs truncado: se divergem, remove o
    RABO de protocolo do truncado e re-anexa a forma canônica.

    Remoção é TRAILING-only e com gate (review pré-push 2026-07-15 — a 1ª
    versão removia por regex em qualquer posição e deletava prosa legítima
    'Decision: Approve the refund...'): uma linha final só sai quando (a) é
    protocolo com par VÁLIDO contra o schema, (b) é prefixo EXATO de uma linha
    de protocolo do original (o truncate corta por posição — o rabo cortado é
    sempre prefixo literal da linha original), ou (c) é o fragmento 'DECIS…'
    cortado antes do ':'. Prosa e citações do formato no meio do texto ficam.
    O texto pode exceder o preset em ~1 linha — deliberado: preservar o
    contrato vale mais que o limite exato.
    """
    if not schema:
        return truncated
    orig = extract_decision_line(original, schema)
    if not orig:
        return truncated
    if extract_decision_line(truncated, schema) == orig:
        return truncated
    # Linhas de protocolo CRUAS do original — reconhecem o rabo cortado por
    # prefixo literal ('DECISAO: escalar=sim; severida' ⊂ linha original).
    orig_proto = [ln.rstrip() for ln in original.splitlines() if _DECISION_LINE_RE.match(ln)]
    lines = truncated.splitlines()
    end = len(lines)
    while end > 0:
        ln = lines[end - 1]
        if not ln.strip():
            end -= 1
            continue
        tail = ln.rstrip().rstrip("…").rstrip()
        is_valid_proto = bool(_DECISION_LINE_RE.match(ln)) and bool(extract_decision_line(ln, schema))
        is_cut_body = bool(tail) and any(o.startswith(tail) for o in orig_proto)
        if is_valid_proto or is_cut_body or _is_cut_decision_prefix(ln):
            end -= 1
            continue
        break
    base = "\n".join(lines[:end]).rstrip()
    return (base + "\n" if base else "") + build_decision_line(orig)


def strip_decision_line(text: str, schema: Optional[dict]) -> str:
    """Remove a(s) linha(s) FINAIS `DECISAO:` da resposta APRESENTADA ao usuário
    (decisão de design 2026-07-15: a linha é protocolo de máquina — o gate já a
    leu e o trace a preserva; o usuário final não vê jargão).

    Gate DUPLO contra falso-positivo (blocker do review do plano): sem `schema`
    → no-op; e a linha só sai se o corpo valida ≥1 par contra o schema — prosa
    legítima de agente SEM contrato ('Decisão: aprovado o crédito') fica
    intacta. Só linhas FINAIS: citação do formato no meio do texto não é
    tocada. NUNCA esvazia: se não sobrar conteúdo, devolve o texto original.
    """
    if not text or not schema:
        return text
    lines = text.splitlines()
    end = len(lines)
    removed = False
    while end > 0:
        ln = lines[end - 1]
        if not ln.strip():
            end -= 1
            continue
        if _DECISION_LINE_RE.match(ln) and extract_decision_line(ln, schema):
            end -= 1
            removed = True
            continue
        break
    if not removed:
        return text
    result = "\n".join(lines[:end]).rstrip()
    return result if result.strip() else text
